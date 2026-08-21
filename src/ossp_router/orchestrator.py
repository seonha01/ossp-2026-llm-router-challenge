# SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
# SPDX-License-Identifier: Apache-2.0

"""Official per-tier orchestration with immutable images and ID/order audits."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Mapping, Optional, Sequence, Tuple

from .protocol import (
    TIERS,
    Episode,
    InputBatch,
    ProtocolError,
    RoutingPolicy,
    Submission,
    dumps_json,
    load_bundled_policy,
    load_input,
    load_policy,
    policy_sha256,
)
from .runtime import (
    DEFAULT_MAX_INFRASTRUCTURE_FAILURES,
    OPERATOR_HELPER_IMAGE,
    OPERATOR_HELPER_PATH,
    AttemptKind,
    AttemptResult,
    DeclaredVolumeNotAllowed,
    ImageLimitExceeded,
    ImagePreflightRejected,
    ImageSizeEvidence,
    InfrastructureUnavailable,
    OFFICIAL_CONTAINER_PLATFORM,
    PendingRuntimeResources,
    PHASE_C_CANDIDATE_LIMITS,
    RetryOutcome,
    exclusive_evaluation_lock,
    is_immutable_image_reference,
    is_named_image_digest,
    inspect_image_runtime_metadata,
    load_image_size_evidence,
    prepare_operator_directory,
    require_no_pending_runtime_resources,
    run_isolated_container_once,
    run_with_retry,
    validate_image_configuration,
    validate_image_size_evidence,
    validate_operator_directory,
    validate_runtime_submission,
)


_COMMIT_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_EXIT_CONFIGURATION_ERROR = 2
_EXIT_INFRASTRUCTURE_UNAVAILABLE = 4
_EXIT_REVIEW_REQUIRED = 5
_EXIT_SUBMISSION_PREFLIGHT_REJECTED = 6
_EXECUTION_RECORD_MAX_BYTES = 16 * 1024 * 1024
_STATUS_EXIT_CODES = {
    "valid": 0,
    "tier_score_zero": 0,
    "disqualified": 0,
    "infrastructure_unavailable": _EXIT_INFRASTRUCTURE_UNAVAILABLE,
    "audit_inconclusive_infrastructure": _EXIT_INFRASTRUCTURE_UNAVAILABLE,
    "review_required": _EXIT_REVIEW_REQUIRED,
    "audit_skipped": _EXIT_REVIEW_REQUIRED,
}


@dataclass(frozen=True)
class ResolvedImage:
    submitted_digest: str
    local_image_id: str
    operating_system: str
    architecture: str
    selected_manifest_digest: str
    oci_compressed_layer_bytes: int
    rootfs_apparent_bytes: int
    size_evidence_sha256: str
    execution_image_reference: Optional[str] = None


@dataclass(frozen=True)
class AuditVariant:
    inputs: InputBatch
    original_to_audit_id: Tuple[Tuple[str, str], ...]


@dataclass(frozen=True)
class AuditMismatch:
    original_episode_id: str
    audit_episode_id: str
    official_model_id: str
    audit_model_id: str


class AuditStatus(str, Enum):
    PASSED = "passed"
    REVIEW_REQUIRED = "review_required"
    INCONCLUSIVE_INFRASTRUCTURE = "inconclusive_infrastructure"


@dataclass(frozen=True)
class AuditOutcome:
    status: AuditStatus
    detail: str
    variant: AuditVariant
    input_sha256: str
    history: Tuple[AttemptResult, ...]
    infrastructure_failures: int
    mismatches: Tuple[AuditMismatch, ...] = ()


@dataclass(frozen=True)
class OfficialTierOutcome:
    commit_sha: str
    image: ResolvedImage
    tier: str
    input_sha256: str
    policy_id: str
    policy_sha256: str
    retry: RetryOutcome
    audit: Optional[AuditOutcome]


@dataclass(frozen=True)
class _PrivateInputSnapshot:
    path: Path
    sha256: str


ImageResolver = Callable[
    [Sequence[str], str, Optional[ImageSizeEvidence]],
    ResolvedImage,
]
ContainerRunner = Callable[..., AttemptResult]


def _inspect_image(
    runtime_command: Sequence[str],
    image: str,
    *,
    platform: str,
) -> Mapping[str, Any]:
    if not runtime_command:
        raise ValueError("컨테이너 실행 명령은 비어 있을 수 없습니다.")
    return inspect_image_runtime_metadata(
        runtime_command,
        image,
        platform=platform,
    )


def resolve_submitted_image(
    runtime_command: Sequence[str],
    submitted_digest: str,
    size_evidence: Optional[ImageSizeEvidence] = None,
) -> ResolvedImage:
    """Resolve one digest and require operator-produced platform size evidence."""

    if not is_named_image_digest(submitted_digest):
        raise ValueError(
            "공식 이미지에는 repository@sha256:<64자리 소문자 16진수> "
            "형식의 다이제스트가 필요합니다."
        )
    if size_evidence is None:
        raise InfrastructureUnavailable(
            "공식 전처리에서 생성한 이미지 크기 증거가 없습니다."
        )
    value = _inspect_image(
        runtime_command,
        submitted_digest,
        platform=OFFICIAL_CONTAINER_PLATFORM,
    )
    try:
        local_id = value["Id"]
        repository_digests = value["RepoDigests"]
        operating_system = value["Os"]
        architecture = value["Architecture"]
    except (KeyError, TypeError) as exc:
        raise InfrastructureUnavailable(
            f"운영자가 이미지 검사 결과를 해석할 수 없습니다: {exc}"
        ) from exc
    validate_image_configuration(value)
    if (
        not isinstance(local_id, str)
        or not is_immutable_image_reference(local_id)
        or not isinstance(repository_digests, list)
        or submitted_digest not in repository_digests
    ):
        raise InfrastructureUnavailable(
            "제출 다이제스트와 로컬 이미지 ID의 대응을 확인할 수 없습니다."
        )
    if not isinstance(operating_system, str) or not isinstance(
        architecture, str
    ):
        raise InfrastructureUnavailable("이미지 플랫폼 정보가 올바르지 않습니다.")
    validate_image_size_evidence(
        size_evidence,
        submitted_digest=submitted_digest,
        local_image_id=local_id,
        operating_system=operating_system,
        architecture=architecture,
    )
    return ResolvedImage(
        submitted_digest=submitted_digest,
        local_image_id=local_id,
        operating_system=operating_system,
        architecture=architecture,
        selected_manifest_digest=size_evidence.selected_manifest_digest,
        oci_compressed_layer_bytes=size_evidence.oci_compressed_layer_bytes,
        rootfs_apparent_bytes=size_evidence.rootfs_apparent_bytes,
        size_evidence_sha256=size_evidence.evidence_sha256,
        execution_image_reference=submitted_digest,
    )


def _audit_token(secret: bytes, domain: bytes, index: int, episode_id: str) -> bytes:
    material = (
        domain
        + index.to_bytes(8, "big")
        + b"\0"
        + episode_id.encode("utf-8")
    )
    return hmac.new(secret, material, hashlib.sha256).digest()


def make_id_order_audit_variant(
    inputs: InputBatch,
    *,
    secret: Optional[bytes] = None,
) -> AuditVariant:
    """Secretly reassign IDs and order while preserving every occurrence."""

    key = secrets.token_bytes(32) if secret is None else secret
    if len(key) < 16:
        raise ValueError("감사 비밀값은 16바이트 이상이어야 합니다.")
    assigned_ids = [episode.episode_id for episode in inputs.episodes]
    if len(inputs.episodes) > 1:
        id_cycle = sorted(
            range(len(inputs.episodes)),
            key=lambda index: (
                _audit_token(
                    key,
                    b"id-cycle",
                    index,
                    inputs.episodes[index].episode_id,
                ),
                index,
            ),
        )
        for offset, index in enumerate(id_cycle):
            successor = id_cycle[(offset + 1) % len(id_cycle)]
            assigned_ids[index] = inputs.episodes[successor].episode_id
    rows = []
    mapping = []
    for index, episode in enumerate(inputs.episodes):
        identifier = assigned_ids[index]
        order = _audit_token(key, b"order", index, episode.episode_id)
        changed = Episode(
            episode_id=identifier,
            prompt=episode.prompt,
            messages=episode.messages,
        )
        rows.append((order, index, changed))
        mapping.append((episode.episode_id, identifier))
    rows.sort(key=lambda item: (item[0], item[1]))
    if len(rows) > 1 and [item[1] for item in rows] == list(range(len(rows))):
        rows = rows[1:] + rows[:1]
    variant = InputBatch(
        schema_version=inputs.schema_version,
        challenge_id=inputs.challenge_id,
        split=inputs.split,
        episodes=tuple(item[2] for item in rows),
    )
    return AuditVariant(variant, tuple(mapping))


def input_batch_to_dict(inputs: InputBatch) -> Dict[str, Any]:
    episodes = []
    for episode in inputs.episodes:
        raw: Dict[str, Any] = {"episode_id": episode.episode_id}
        if episode.prompt is not None:
            raw["prompt"] = episode.prompt
        else:
            raw["messages"] = [
                {"role": message.role, "content": message.content}
                for message in episode.messages or ()
            ]
        episodes.append(raw)
    return {
        "schema_version": inputs.schema_version,
        "challenge_id": inputs.challenge_id,
        "split": inputs.split,
        "episodes": episodes,
    }


def run_id_order_audit(
    *,
    original_inputs: InputBatch,
    official_submission: Submission,
    policy: RoutingPolicy,
    tier: str,
    attempt: Callable[[InputBatch, int], AttemptResult],
    secret: Optional[bytes] = None,
    max_infrastructure_failures: int = DEFAULT_MAX_INFRASTRUCTURE_FAILURES,
) -> AuditOutcome:
    """Execute and compare an audit without consuming official retry attempts."""

    if max_infrastructure_failures < 1:
        raise ValueError("max_infrastructure_failures는 1 이상이어야 합니다.")
    validate_runtime_submission(
        original_inputs,
        official_submission,
        policy,
        tier,
    )
    variant = make_id_order_audit_variant(original_inputs, secret=secret)
    audit_input_sha256 = hashlib.sha256(
        dumps_json(input_batch_to_dict(variant.inputs)).encode("utf-8")
    ).hexdigest()
    history = []
    infrastructure_failures = 0
    while True:
        result = attempt(variant.inputs, infrastructure_failures + 1)
        history.append(result)
        if result.kind is AttemptKind.INFRASTRUCTURE_FAILURE:
            infrastructure_failures += 1
            if (
                result.cleanup_pending
                or infrastructure_failures >= max_infrastructure_failures
            ):
                return AuditOutcome(
                    AuditStatus.INCONCLUSIVE_INFRASTRUCTURE,
                    "운영자 인프라 장애로 감사 비교를 완료하지 못했습니다.",
                    variant,
                    audit_input_sha256,
                    tuple(history),
                    infrastructure_failures,
                )
            continue
        if result.kind is not AttemptKind.VALID or result.submission is None:
            return AuditOutcome(
                AuditStatus.REVIEW_REQUIRED,
                "감사 실행이 유효한 결과를 만들지 못했습니다.",
                variant,
                audit_input_sha256,
                tuple(history),
                infrastructure_failures,
            )
        official_models = {
            decision.episode_id: decision.model_id
            for decision in official_submission.decisions
        }
        audit_models = {
            decision.episode_id: decision.model_id
            for decision in result.submission.decisions
        }
        mismatches = []
        for original_id, audit_id in variant.original_to_audit_id:
            official_model = official_models[original_id]
            audit_model = audit_models[audit_id]
            if official_model != audit_model:
                mismatches.append(
                    AuditMismatch(
                        original_id,
                        audit_id,
                        official_model,
                        audit_model,
                    )
                )
        if mismatches:
            return AuditOutcome(
                AuditStatus.REVIEW_REQUIRED,
                (
                    "문항 ID와 순서만 바꾼 감사에서 "
                    f"{len(mismatches)}개 선택이 달라졌습니다."
                ),
                variant,
                audit_input_sha256,
                tuple(history),
                infrastructure_failures,
                tuple(mismatches),
            )
        return AuditOutcome(
            AuditStatus.PASSED,
            "문항 ID와 순서 변경 전후의 선택이 모두 일치합니다.",
            variant,
            audit_input_sha256,
            tuple(history),
            infrastructure_failures,
        )


def _attempt_to_dict(
    result: AttemptResult,
    *,
    sequence: int,
    official_attempt: Optional[int] = None,
) -> Dict[str, Any]:
    stdout = result.stdout.encode("utf-8")
    stderr = result.stderr.encode("utf-8")
    if result.kind is AttemptKind.VALID:
        reason_code = "valid_submission"
    elif result.kind is AttemptKind.FAIRNESS_VIOLATION:
        reason_code = "fairness_review_required"
    elif result.cleanup_pending:
        reason_code = "operator_cleanup_pending"
    elif result.kind is AttemptKind.INFRASTRUCTURE_FAILURE:
        reason_code = "operator_infrastructure_failure"
    elif result.timed_out:
        reason_code = "wall_time_limit_exceeded"
    elif result.operator_terminated:
        reason_code = "operator_limit_terminated"
    elif result.returncode not in (None, 0):
        reason_code = "participant_nonzero_exit"
    else:
        reason_code = "submission_validation_failure"
    value: Dict[str, Any] = {
        "sequence": sequence,
        "kind": result.kind.value,
        "reason_code": reason_code,
        "returncode": result.returncode,
        "timed_out": result.timed_out,
        "operator_terminated": result.operator_terminated,
        "cleanup_pending": result.cleanup_pending,
        "stdout_normalized_utf8_bytes": len(stdout),
        "stdout_normalized_utf8_sha256": hashlib.sha256(stdout).hexdigest(),
        "stderr_normalized_utf8_bytes": len(stderr),
        "stderr_normalized_utf8_sha256": hashlib.sha256(stderr).hexdigest(),
        "output_bytes": result.output_bytes,
        "output_sha256": result.output_sha256,
        "cleanup_attempt_id": result.cleanup_attempt_id,
    }
    if official_attempt is not None:
        value["official_attempt"] = official_attempt
    return value


def _official_tier_status(outcome: OfficialTierOutcome) -> str:
    """Return the single status used by both records and scheduler exit codes."""

    if outcome.retry.infrastructure_unavailable:
        return "infrastructure_unavailable"
    if outcome.retry.disqualified:
        return "disqualified"
    if outcome.retry.tier_score_zero:
        return "tier_score_zero"
    if (
        outcome.audit is not None
        and outcome.audit.status is AuditStatus.REVIEW_REQUIRED
    ):
        return "review_required"
    if (
        outcome.audit is not None
        and outcome.audit.status is AuditStatus.INCONCLUSIVE_INFRASTRUCTURE
    ):
        return "audit_inconclusive_infrastructure"
    if outcome.audit is None:
        return "audit_skipped"
    return "valid"


def official_tier_outcome_to_dict(outcome: OfficialTierOutcome) -> Dict[str, Any]:
    attempt_number = 1
    history = []
    for sequence, result in enumerate(outcome.retry.history, start=1):
        history.append(
            _attempt_to_dict(
                result,
                sequence=sequence,
                official_attempt=attempt_number,
            )
        )
        if result.kind in (AttemptKind.EXECUTION_FAILURE, AttemptKind.VALID):
            attempt_number += 1
    audit = None
    if outcome.audit is not None:
        audit = {
            "status": outcome.audit.status.value,
            "detail": outcome.audit.detail,
            "input_sha256": outcome.audit.input_sha256,
            "infrastructure_failures": outcome.audit.infrastructure_failures,
            "episode_id_mapping": [
                {
                    "original_episode_id": original_id,
                    "audit_episode_id": audit_id,
                }
                for original_id, audit_id
                in outcome.audit.variant.original_to_audit_id
            ],
            "history": [
                _attempt_to_dict(result, sequence=index)
                for index, result in enumerate(outcome.audit.history, start=1)
            ],
            "mismatches": [
                {
                    "original_episode_id": item.original_episode_id,
                    "audit_episode_id": item.audit_episode_id,
                    "official_model_id": item.official_model_id,
                    "audit_model_id": item.audit_model_id,
                }
                for item in outcome.audit.mismatches
            ],
        }
    return {
        "schema_version": 1,
        "report_type": "official-tier-execution-record",
        "status": _official_tier_status(outcome),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "commit_sha": outcome.commit_sha,
        "image": {
            "submitted_digest": outcome.image.submitted_digest,
            "resolved_local_image_id": outcome.image.local_image_id,
            "execution_image_reference": (
                outcome.image.execution_image_reference
                or outcome.image.local_image_id
            ),
            "os": outcome.image.operating_system,
            "architecture": outcome.image.architecture,
            "selected_manifest_digest": (
                outcome.image.selected_manifest_digest
            ),
            "oci_compressed_layer_bytes": (
                outcome.image.oci_compressed_layer_bytes
            ),
            "rootfs_apparent_bytes": outcome.image.rootfs_apparent_bytes,
            "size_evidence_sha256": outcome.image.size_evidence_sha256,
        },
        "operator_helper": {
            "image": OPERATOR_HELPER_IMAGE,
            "script_sha256": hashlib.sha256(
                OPERATOR_HELPER_PATH.read_bytes()
            ).hexdigest(),
        },
        "tier": outcome.tier,
        "input_sha256": outcome.input_sha256,
        "policy_id": outcome.policy_id,
        "policy_sha256": outcome.policy_sha256,
        "official_attempts": outcome.retry.official_attempts,
        "infrastructure_failures": outcome.retry.infrastructure_failures,
        "infrastructure_unavailable": outcome.retry.infrastructure_unavailable,
        "tier_score_zero": outcome.retry.tier_score_zero,
        "disqualified": outcome.retry.disqualified,
        "history": history,
        "audit": audit,
    }


def _run_official_tier_locked(
    *,
    runtime_command: Sequence[str],
    submitted_image_digest: str,
    commit_sha: str,
    inputs: InputBatch,
    policy: RoutingPolicy,
    input_path: Path,
    input_sha256: str,
    output_directory: Path,
    tier: str,
    audit: bool = True,
    audit_secret: Optional[bytes] = None,
    image_size_evidence: Optional[ImageSizeEvidence] = None,
    image_resolver: ImageResolver = resolve_submitted_image,
    container_runner: ContainerRunner = run_isolated_container_once,
) -> OfficialTierOutcome:
    """Run one already validated official tier while holding its root lock."""

    official_directory = _prepare_output_child_directory(
        output_directory,
        "official",
        "공식 실행 출력 디렉터리",
    )
    audit_directory = _prepare_output_child_directory(
        output_directory,
        "audit",
        "감사 실행 출력 디렉터리",
    )
    require_no_pending_runtime_resources(
        runtime_command,
        (official_directory, audit_directory),
    )

    resolved = image_resolver(
        runtime_command,
        submitted_image_digest,
        image_size_evidence,
    )
    execution_image = (
        resolved.execution_image_reference or resolved.local_image_id
    )
    if (
        not is_immutable_image_reference(resolved.local_image_id)
        or not is_immutable_image_reference(
            resolved.selected_manifest_digest
        )
        or re.fullmatch(
            r"[0-9a-f]{64}",
            resolved.size_evidence_sha256,
        )
        is None
        or not is_immutable_image_reference(execution_image)
    ):
        raise InfrastructureUnavailable(
            "운영자가 확인한 이미지 다이제스트 또는 크기 증거 해시가 "
            "올바르지 않습니다."
        )
    for name, value, maximum in (
        (
            "OCI 압축 계층 합계",
            resolved.oci_compressed_layer_bytes,
            PHASE_C_CANDIDATE_LIMITS.oci_compressed_image_bytes,
        ),
        (
            "rootfs 겉보기 크기",
            resolved.rootfs_apparent_bytes,
            PHASE_C_CANDIDATE_LIMITS.rootfs_apparent_bytes,
        ),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise InfrastructureUnavailable(
                f"운영자가 확인한 {name}가 올바르지 않습니다."
            )
        if value > maximum:
            raise ImageLimitExceeded(
                f"{name}가 공식 한도를 초과합니다."
            )

    def official_attempt(_number: int) -> AttemptResult:
        return container_runner(
            runtime_command,
            image=execution_image,
            platform=(
                f"{resolved.operating_system}/{resolved.architecture}"
            ),
            inputs=inputs,
            policy=policy,
            input_path=input_path,
            output_directory=official_directory,
            tier=tier,
        )

    retry = run_with_retry(official_attempt)
    audit_outcome = None
    if audit and retry.selected_submission is not None:
        def audit_attempt(
            variant_inputs: InputBatch,
            _infrastructure_attempt: int,
        ) -> AttemptResult:
            variant_path = input_path.with_name("audit-input.json")
            try:
                _write_private_json(
                    variant_path,
                    input_batch_to_dict(variant_inputs),
                )
            except InfrastructureUnavailable as exc:
                return AttemptResult(
                    AttemptKind.INFRASTRUCTURE_FAILURE,
                    str(exc),
                )
            return container_runner(
                runtime_command,
                image=execution_image,
                platform=(
                    f"{resolved.operating_system}/{resolved.architecture}"
                ),
                inputs=variant_inputs,
                policy=policy,
                input_path=variant_path,
                output_directory=audit_directory,
                tier=tier,
            )

        audit_outcome = run_id_order_audit(
            original_inputs=inputs,
            official_submission=retry.selected_submission,
            policy=policy,
            tier=tier,
            attempt=audit_attempt,
            secret=audit_secret,
        )
    return OfficialTierOutcome(
        commit_sha=commit_sha,
        image=resolved,
        tier=tier,
        input_sha256=input_sha256,
        policy_id=policy.policy_id,
        policy_sha256=policy_sha256(policy),
        retry=retry,
        audit=audit_outcome,
    )


def _validate_record_path(record_path: Path, output_directory: Path) -> Path:
    try:
        parent = record_path.parent.resolve()
        root = output_directory.resolve()
    except OSError as exc:
        raise ValueError(f"실행 기록 경로를 확인할 수 없습니다: {exc}") from exc
    if (
        parent != root
        or record_path.name.startswith(".")
        or record_path.suffix != ".json"
    ):
        raise ValueError(
            "실행 기록은 등급 출력 디렉터리 바로 아래의 공개 JSON 파일이어야 합니다."
        )
    return root / record_path.name


def _prepare_operator_directory(
    directory: Path,
    label: str,
    *,
    parents: bool = False,
) -> Path:
    return prepare_operator_directory(
        directory,
        label,
        parents=parents,
    )


def _prepare_output_child_directory(
    output_directory: Path,
    name: str,
    label: str,
) -> Path:
    """Prepare one real, direct child below the canonical output root."""

    expected = output_directory / name
    try:
        metadata = expected.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise InfrastructureUnavailable(
            f"{label}를 확인할 수 없습니다: {exc}"
        ) from exc
    else:
        if stat.S_ISLNK(metadata.st_mode):
            raise InfrastructureUnavailable(
                f"{label}는 출력 루트 바로 아래의 실제 디렉터리여야 합니다."
            )

    canonical = _prepare_operator_directory(expected, label)
    if canonical != expected:
        raise InfrastructureUnavailable(
            f"{label}는 출력 루트 바로 아래의 실제 디렉터리여야 합니다."
        )
    return canonical


def _write_private_payload(path: Path, payload: bytes) -> None:
    temporary = path.with_name(
        f".{path.name}.operator-{secrets.token_hex(8)}"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(str(temporary), flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        os.chmod(path, 0o444)
        directory = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        raise InfrastructureUnavailable(
            f"비공개 평가 입력 스냅샷을 기록할 수 없습니다: {exc}"
        ) from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _copy_private_input(source: Path, destination: Path) -> str:
    temporary = destination.with_name(
        f".{destination.name}.operator-{secrets.token_hex(8)}"
    )
    digest = hashlib.sha256()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        with source.open("rb") as input_stream:
            metadata = os.fstat(input_stream.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("공식 입력은 일반 파일이어야 합니다.")
            descriptor = os.open(str(temporary), flags, 0o600)
            try:
                with os.fdopen(descriptor, "wb", closefd=False) as output_stream:
                    while True:
                        chunk = input_stream.read(1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
                        output_stream.write(chunk)
                    output_stream.flush()
                    os.fsync(output_stream.fileno())
            finally:
                os.close(descriptor)
        os.replace(temporary, destination)
        os.chmod(destination, 0o444)
        directory = os.open(str(destination.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        raise InfrastructureUnavailable(
            f"공식 입력을 비공개 스냅샷으로 고정할 수 없습니다: {exc}"
        ) from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return digest.hexdigest()


def _write_private_json(path: Path, value: Mapping[str, Any]) -> None:
    _write_private_payload(path, dumps_json(value).encode("utf-8"))


@contextmanager
def _private_input_snapshot(
    input_path: Path,
    expected_inputs: InputBatch,
    private_work_directory: Path,
) -> Iterator[_PrivateInputSnapshot]:
    try:
        directory = Path(
            tempfile.mkdtemp(
                prefix=".ossp-router-private-input-",
                dir=private_work_directory,
            )
        )
        directory.chmod(0o700)
    except OSError as exc:
        raise InfrastructureUnavailable(
            f"비공개 평가 입력 작업 공간을 만들 수 없습니다: {exc}"
        ) from exc
    try:
        snapshot_path = directory / "official-input.json"
        input_sha256 = _copy_private_input(input_path, snapshot_path)
        if load_input(snapshot_path) != expected_inputs:
            raise ValueError(
                "실행 직전에 고정한 입력과 전달한 입력 객체가 다릅니다."
            )
        yield _PrivateInputSnapshot(snapshot_path, input_sha256)
    finally:
        try:
            shutil.rmtree(directory)
        except OSError as exc:
            raise InfrastructureUnavailable(
                f"비공개 평가 입력 작업 공간을 삭제할 수 없습니다: {exc}"
            ) from exc


def _paths_overlap(first: Path, second: Path) -> bool:
    try:
        first.relative_to(second)
        return True
    except ValueError:
        pass
    try:
        second.relative_to(first)
        return True
    except ValueError:
        return False


def _validate_private_work_directory(
    private_work_directory: Path,
    output_directory: Path,
) -> Path:
    try:
        private_root = validate_operator_directory(
            private_work_directory,
            "비공개 평가 작업 경로",
        )
        output_root = validate_operator_directory(
            output_directory,
            "운영자 평가 출력 디렉터리",
        )
    except InfrastructureUnavailable:
        raise
    except OSError as exc:
        raise InfrastructureUnavailable(
            f"비공개 평가 작업 경로를 확인할 수 없습니다: {exc}"
        ) from exc
    if _paths_overlap(private_root, output_root):
        raise InfrastructureUnavailable(
            "비공개 평가 작업 경로와 결과 출력 경로는 서로 분리해야 합니다."
        )
    try:
        with os.scandir(private_root) as entries:
            if next(entries, None) is not None:
                raise InfrastructureUnavailable(
                    "비공개 평가 작업 경로에 이전 실행의 잔여물이 있습니다."
                )
    except InfrastructureUnavailable:
        raise
    except OSError as exc:
        raise InfrastructureUnavailable(
            f"비공개 평가 작업 경로를 검사할 수 없습니다: {exc}"
        ) from exc
    return private_root


def _record_matches_cleanup_attempt(
    record_path: Path,
    cleanup_attempt_id: Optional[str],
) -> bool:
    if cleanup_attempt_id is None:
        return False
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(str(record_path), flags)
    except (FileNotFoundError, OSError):
        return False
    try:
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_size > _EXECUTION_RECORD_MAX_BYTES
            ):
                return False
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                payload = stream.read(_EXECUTION_RECORD_MAX_BYTES + 1)
        except OSError:
            return False
    finally:
        os.close(descriptor)
    try:
        value = json.loads(payload)
    except (RecursionError, UnicodeError, json.JSONDecodeError):
        return False
    if (
        not isinstance(value, dict)
        or isinstance(value.get("schema_version"), bool)
        or value.get("schema_version") != 1
        or value.get("report_type") != "official-tier-execution-record"
        or value.get("status")
        not in {
            "infrastructure_unavailable",
            "audit_inconclusive_infrastructure",
        }
    ):
        return False
    if (
        value.get("cleanup_pending") is True
        and value.get("cleanup_attempt_id") == cleanup_attempt_id
    ):
        return True
    histories = [value.get("history")]
    audit = value.get("audit")
    if isinstance(audit, dict):
        histories.append(audit.get("history"))
    return any(
        isinstance(history, list)
        and any(
            isinstance(item, dict)
            and item.get("cleanup_pending") is True
            and item.get("cleanup_attempt_id") == cleanup_attempt_id
            for item in history
        )
        for history in histories
    )


def _write_record_atomic(record_path: Path, value: Mapping[str, Any]) -> None:
    temporary = record_path.with_name(
        f".{record_path.name}.operator-{secrets.token_hex(8)}"
    )
    payload = dumps_json(value).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(str(temporary), flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(descriptor)
        os.chmod(temporary, 0o644)
        os.replace(temporary, record_path)
        directory = os.open(str(record_path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        raise InfrastructureUnavailable(
            f"구조화된 실행 기록을 원자적으로 게시할 수 없습니다: {exc}"
        ) from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def run_official_tier(
    *,
    runtime_command: Sequence[str],
    submitted_image_digest: str,
    commit_sha: str,
    inputs: InputBatch,
    policy: RoutingPolicy,
    input_path: Path,
    output_directory: Path,
    private_work_directory: Path,
    tier: str,
    audit: bool = True,
    audit_secret: Optional[bytes] = None,
    image_size_evidence: Optional[ImageSizeEvidence] = None,
    image_resolver: ImageResolver = resolve_submitted_image,
    container_runner: ContainerRunner = run_isolated_container_once,
    record_path: Optional[Path] = None,
) -> OfficialTierOutcome:
    """Run and optionally publish its record under one output-root lock."""

    output_directory = _prepare_operator_directory(
        output_directory,
        "운영자 평가 출력 디렉터리",
        parents=True,
    )
    with exclusive_evaluation_lock(output_directory) as output_directory:
        if record_path is not None:
            record_path = _validate_record_path(record_path, output_directory)
        try:
            if _COMMIT_SHA.fullmatch(commit_sha) is None:
                raise ValueError(
                    "평가 커밋 SHA는 40자 또는 64자 소문자 16진수여야 합니다."
                )
            if tier not in TIERS:
                raise ValueError(f"알 수 없는 실행 등급: {tier}")
            private_work_directory = _validate_private_work_directory(
                private_work_directory,
                output_directory,
            )
            with _private_input_snapshot(
                input_path,
                inputs,
                private_work_directory,
            ) as snapshot:
                outcome = _run_official_tier_locked(
                    runtime_command=runtime_command,
                    submitted_image_digest=submitted_image_digest,
                    commit_sha=commit_sha,
                    inputs=inputs,
                    policy=policy,
                    input_path=snapshot.path,
                    input_sha256=snapshot.sha256,
                    output_directory=output_directory,
                    tier=tier,
                    audit=audit,
                    audit_secret=audit_secret,
                    image_size_evidence=image_size_evidence,
                    image_resolver=image_resolver,
                    container_runner=container_runner,
                )
        except PendingRuntimeResources as exc:
            if record_path is not None and not _record_matches_cleanup_attempt(
                record_path,
                exc.cleanup_attempt_id,
            ):
                pending_record: Dict[str, Any] = {
                    "schema_version": 1,
                    "report_type": "official-tier-execution-record",
                    "status": "infrastructure_unavailable",
                    "reason_code": "operator_cleanup_pending",
                    "cleanup_pending": True,
                }
                if exc.cleanup_attempt_id is not None:
                    pending_record["cleanup_attempt_id"] = (
                        exc.cleanup_attempt_id
                    )
                _write_record_atomic(record_path, pending_record)
            raise
        except ImagePreflightRejected as exc:
            if record_path is not None:
                reason_code = (
                    "declared_volume_not_allowed"
                    if isinstance(exc, DeclaredVolumeNotAllowed)
                    else "image_size_limit_exceeded"
                )
                _write_record_atomic(
                    record_path,
                    {
                        "schema_version": 1,
                        "report_type": "official-tier-execution-record",
                        "status": "submission_preflight_rejected",
                        "reason_code": reason_code,
                    },
                )
            raise
        except InfrastructureUnavailable:
            if record_path is not None:
                _write_record_atomic(
                    record_path,
                    {
                        "schema_version": 1,
                        "report_type": "official-tier-execution-record",
                        "status": "infrastructure_unavailable",
                        "reason_code": "operator_infrastructure_unavailable",
                    },
                )
            raise
        except (OSError, ProtocolError, ValueError):
            if record_path is not None:
                _write_record_atomic(
                    record_path,
                    {
                        "schema_version": 1,
                        "report_type": "official-tier-execution-record",
                        "status": "configuration_error",
                        "reason_code": "invalid_operator_configuration",
                    },
                )
            raise
        if record_path is not None:
            _write_record_atomic(
                record_path,
                official_tier_outcome_to_dict(outcome),
            )
        return outcome


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="router-evaluate-tier",
        description="고정 이미지로 한 등급을 공식 재시도·감사 경로에서 실행합니다.",
    )
    parser.add_argument("--image", required=True)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--tier", choices=TIERS, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--private-work-directory", type=Path, required=True)
    parser.add_argument("--image-size-evidence", type=Path, required=True)
    parser.add_argument("--record", type=Path, required=True)
    parser.add_argument("--runtime", default="docker")
    parser.add_argument("--docker-context")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    runtime = [args.runtime]
    if args.docker_context:
        runtime.extend(["--context", args.docker_context])
    try:
        inputs = load_input(args.input)
        policy = load_policy(args.policy) if args.policy else load_bundled_policy()
        image_size_evidence = load_image_size_evidence(
            args.image_size_evidence
        )
        outcome = run_official_tier(
            runtime_command=runtime,
            submitted_image_digest=args.image,
            commit_sha=args.commit_sha,
            inputs=inputs,
            policy=policy,
            input_path=args.input,
            output_directory=args.output_directory,
            private_work_directory=args.private_work_directory,
            tier=args.tier,
            audit=True,
            image_size_evidence=image_size_evidence,
            record_path=args.record,
        )
    except ImagePreflightRejected:
        return _EXIT_SUBMISSION_PREFLIGHT_REJECTED
    except InfrastructureUnavailable:
        return _EXIT_INFRASTRUCTURE_UNAVAILABLE
    except (OSError, ProtocolError, ValueError):
        return _EXIT_CONFIGURATION_ERROR
    return _STATUS_EXIT_CODES[_official_tier_status(outcome)]


if __name__ == "__main__":
    raise SystemExit(main())
