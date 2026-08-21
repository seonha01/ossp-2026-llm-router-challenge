# SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
# SPDX-License-Identifier: Apache-2.0

"""Operator-side execution validation and bounded participant retries."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import selectors
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from .protocol import (
    TIERS,
    InputBatch,
    ProtocolError,
    RoutingPolicy,
    Submission,
    load_submission,
)


DEFAULT_MAX_PARTICIPANT_ATTEMPTS = 3
DEFAULT_MAX_INFRASTRUCTURE_FAILURES = 3
DEFAULT_MAX_OUTPUT_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_LOG_BYTES = 256 * 1024
DEFAULT_MAX_LOG_EMITTED_BYTES = 1024 * 1024
DEFAULT_TERMINATION_GRACE_SECONDS = 5.0
OFFICIAL_CONTAINER_OS = "linux"
OFFICIAL_CONTAINER_ARCHITECTURE = "arm64"
OFFICIAL_CONTAINER_PLATFORM = (
    f"{OFFICIAL_CONTAINER_OS}/{OFFICIAL_CONTAINER_ARCHITECTURE}"
)
RESOURCE_JOURNAL_NAME = ".ossp-router-resource-journal.v1.json"
RESOURCE_LOCK_NAME = ".ossp-router-resource.lock"
RESOURCE_JOURNAL_MAX_BYTES = 16 * 1024
IMAGE_SIZE_EVIDENCE_MAX_BYTES = 16 * 1024
CONTAINER_INPUT_PATH = "/challenge/input/inputs.json"
CONTAINER_OUTPUT_DIRECTORY = "/challenge/output"
CONTAINER_OUTPUT_PATH = f"{CONTAINER_OUTPUT_DIRECTORY}/submission.json"
CONTAINER_TEMPORARY_DIRECTORY = "/tmp"
CONTAINER_USER = "65532:65532"
OPERATOR_HELPER_IMAGE = (
    "python:3.14.6-alpine3.23@sha256:"
    "b165067c5afc37fa5608a3c05609cc3d51aafd808a30fbfd822ee594fef55ad4"
)
OPERATOR_HELPER_PATH = Path(__file__).with_name("operator_helper.py")
OPERATOR_RESULT_DIRECTORY = "/operator/result"
OPERATOR_HELPER_SCRIPT = f"{OPERATOR_RESULT_DIRECTORY}/operator_helper.py"
OUTPUT_VOLUME_INODE_LIMIT = 64
ATTEMPT_LABEL = "org.sktelecom.ossp-router.attempt"
_NAMED_IMAGE_DIGEST = re.compile(r"^(.+)@sha256:([0-9a-f]{64})$")
_LOCAL_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_REPOSITORY_COMPONENT = re.compile(
    r"^[a-z0-9]+(?:(?:[._]|__|[-]+)[a-z0-9]+)*$"
)
_REGISTRY_COMPONENT = re.compile(
    r"^(?:localhost|[a-z0-9]+(?:[.-][a-z0-9]+)*)(?::[0-9]+)?$"
)
OCI_LAYER_MEASUREMENT_METHOD = "oci-manifest-layer-descriptors-v1"
ROOTFS_MEASUREMENT_METHOD = "docker-export-tar-apparent-size-v1"


@dataclass(frozen=True)
class RuntimeResourceLimits:
    """Official limits for one container run and one tier."""

    cpu_cores: int
    wall_time_seconds: int
    memory_bytes: int
    memory_swap_total_bytes: int
    processes_and_threads_total: int
    temporary_storage_bytes: int
    output_bytes: int
    output_inodes: int
    retained_log_bytes_per_stream: int
    emitted_log_bytes_per_stream: int
    oci_compressed_image_bytes: int
    rootfs_apparent_bytes: int


@dataclass(frozen=True)
class ImageSizeEvidence:
    """Operator-produced size measurements bound to one platform manifest."""

    submitted_digest: str
    selected_manifest_digest: str
    config_digest: str
    operating_system: str
    architecture: str
    oci_compressed_layer_bytes: int
    rootfs_apparent_bytes: int
    oci_layer_measurement_method: str
    rootfs_measurement_method: str
    evidence_sha256: str


OFFICIAL_RESOURCE_LIMITS = RuntimeResourceLimits(
    cpu_cores=2,
    wall_time_seconds=90,
    memory_bytes=2 * 1024 * 1024 * 1024,
    memory_swap_total_bytes=2 * 1024 * 1024 * 1024,
    processes_and_threads_total=32,
    temporary_storage_bytes=256 * 1024 * 1024,
    output_bytes=DEFAULT_MAX_OUTPUT_BYTES,
    output_inodes=OUTPUT_VOLUME_INODE_LIMIT,
    retained_log_bytes_per_stream=DEFAULT_MAX_LOG_BYTES,
    emitted_log_bytes_per_stream=DEFAULT_MAX_LOG_EMITTED_BYTES,
    oci_compressed_image_bytes=1024 * 1024 * 1024,
    rootfs_apparent_bytes=2 * 1024 * 1024 * 1024,
)

# Compatibility name retained for downstream code written during public
# preparation. Both names refer to the same frozen v1 limits.
PHASE_C_CANDIDATE_LIMITS = OFFICIAL_RESOURCE_LIMITS


class AttemptKind(str, Enum):
    VALID = "valid"
    EXECUTION_FAILURE = "execution_failure"
    INFRASTRUCTURE_FAILURE = "infrastructure_failure"
    FAIRNESS_VIOLATION = "fairness_violation"


@dataclass(frozen=True)
class AttemptResult:
    kind: AttemptKind
    detail: str
    submission: Optional[Submission] = None
    returncode: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    operator_terminated: bool = False
    cleanup_pending: bool = False
    output_bytes: Optional[int] = None
    output_sha256: Optional[str] = None
    cleanup_attempt_id: Optional[str] = None
    measurement_elapsed_ns: Optional[int] = None


@dataclass(frozen=True)
class RetryOutcome:
    selected_submission: Optional[Submission]
    official_attempts: int
    infrastructure_failures: int
    history: Tuple[AttemptResult, ...]
    tier_score_zero: bool
    disqualified: bool
    infrastructure_unavailable: bool = False


class InfrastructureUnavailable(RuntimeError):
    """Raised when evaluation cannot proceed because the operator is unavailable."""


class ImagePreflightRejected(ValueError):
    """Raised when a submitted image is rejected before an official attempt."""


class ImageLimitExceeded(ImagePreflightRejected):
    """Raised when a submitted image fails candidate size preflight."""


class DeclaredVolumeNotAllowed(ImagePreflightRejected):
    """Raised when an image declares quota-bypassing Docker volumes."""


class ResourceLockUnavailable(InfrastructureUnavailable):
    """Raised when another evaluator owns the output-directory lock."""


class PendingRuntimeResources(InfrastructureUnavailable):
    """Raised when an earlier official or audit mutation still needs cleanup."""

    def __init__(
        self,
        detail: str,
        *,
        cleanup_attempt_id: Optional[str] = None,
    ) -> None:
        super().__init__(detail)
        self.cleanup_attempt_id = cleanup_attempt_id


def validate_runtime_submission(
    inputs: InputBatch,
    submission: Submission,
    policy: RoutingPolicy,
    tier: str,
) -> None:
    """Validate all metadata and require exactly one decision per input episode."""

    if submission.schema_version != inputs.schema_version:
        raise ProtocolError("submission과 입력의 schema_version이 다릅니다.")
    if submission.challenge_id != inputs.challenge_id:
        raise ProtocolError("submission과 입력의 challenge_id가 다릅니다.")
    if submission.policy_id != policy.policy_id:
        raise ProtocolError("submission과 정책의 policy_id가 다릅니다.")
    if submission.split != inputs.split:
        raise ProtocolError("submission과 입력의 split이 다릅니다.")
    if submission.tier != tier:
        raise ProtocolError(
            f"submission.tier가 실행 등급과 다릅니다: {submission.tier}"
        )
    expected = {episode.episode_id for episode in inputs.episodes}
    actual = {decision.episode_id for decision in submission.decisions}
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing or extra:
        raise ProtocolError(
            f"decision 범위 오류: 누락={missing}, 초과={extra}"
        )


def _bounded_log(value: object, maximum: int) -> str:
    if isinstance(value, bytes):
        text_value = value.decode("utf-8", errors="replace")
    else:
        text_value = str(value)
    if maximum < 0:
        raise ValueError("maximum은 0 이상이어야 합니다.")
    encoded = text_value.encode("utf-8")
    if len(encoded) <= maximum:
        return text_value
    if maximum == 0:
        return ""
    suffix = "\n...[운영자 로그 한도에서 잘림]".encode("utf-8")
    if len(suffix) >= maximum:
        return encoded[:maximum].decode("utf-8", errors="ignore")
    retained = encoded[: maximum - len(suffix)]
    return (
        retained.decode("utf-8", errors="ignore")
        + suffix.decode("utf-8")
    )


class _BoundedPipeCapture:
    """Retain a fixed prefix while continuously draining an attached pipe."""

    def __init__(self, retained: int, emitted: int) -> None:
        self.retained = retained
        self.emitted = emitted
        self.data = bytearray()
        self.total_bytes = 0
        self.truncated = False

    def feed(self, chunk: bytes) -> None:
        self.total_bytes += len(chunk)
        available = max(0, self.retained + 1 - len(self.data))
        if available:
            self.data.extend(chunk[:available])
        if len(chunk) > available or len(self.data) > self.retained:
            self.truncated = True

    @property
    def emission_exceeded(self) -> bool:
        return self.total_bytes > self.emitted

    def text(self) -> str:
        return _bounded_log(
            bytes(self.data[: self.retained]),
            self.retained,
        )

    def raw(self) -> bytes:
        return bytes(self.data[: self.retained])


def _drain_ready_pipes(
    selector: selectors.BaseSelector,
    captures: Dict[int, _BoundedPipeCapture],
    timeout: float,
) -> int:
    """Drain a bounded amount per ready pipe without waiting for descendants."""

    drained = 0
    for key, _mask in selector.select(timeout):
        descriptor = key.fd
        try:
            chunk = os.read(descriptor, 64 * 1024)
        except BlockingIOError:
            continue
        except OSError:
            chunk = b""
        if not chunk:
            try:
                selector.unregister(descriptor)
            except (KeyError, ValueError):
                pass
            continue
        captures[descriptor].feed(chunk)
        drained += len(chunk)
    return drained


def _finalize_pipe_capture(
    selector: selectors.BaseSelector,
    process: subprocess.Popen[bytes],
    captures: Dict[int, _BoundedPipeCapture],
) -> Tuple[str, str]:
    """Drain bytes already available, then close without waiting for inherited FDs."""

    for _ in range(8):
        if _drain_ready_pipes(selector, captures, 0) == 0:
            break
    stdout = (
        captures[process.stdout.fileno()].text()
        if process.stdout is not None
        else ""
    )
    stderr = (
        captures[process.stderr.fileno()].text()
        if process.stderr is not None
        else ""
    )
    selector.close()
    if process.stdout is not None:
        process.stdout.close()
    if process.stderr is not None:
        process.stderr.close()
    return stdout, stderr


def _signal_process_group(
    process: subprocess.Popen[bytes],
    sig: signal.Signals,
) -> None:
    """Signal the child session, tolerating a process that exited concurrently."""

    try:
        os.killpg(process.pid, sig)
    except (PermissionError, ProcessLookupError):
        pass


def _terminate_process_group(
    process: subprocess.Popen[bytes],
    termination_grace_seconds: float,
) -> None:
    if process.poll() is not None:
        return
    _signal_process_group(process, signal.SIGTERM)
    try:
        process.wait(timeout=termination_grace_seconds)
        return
    except subprocess.TimeoutExpired:
        _signal_process_group(process, signal.SIGKILL)
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        # A task stuck in an uninterruptible kernel state must not hold the
        # evaluator forever.
        pass


def _docker_size(value: int) -> str:
    gibibyte = 1024 * 1024 * 1024
    mebibyte = 1024 * 1024
    if value % gibibyte == 0:
        return f"{value // gibibyte}g"
    if value % mebibyte == 0:
        return f"{value // mebibyte}m"
    return str(value)


def is_named_image_digest(value: str) -> bool:
    """Return whether value is a registry/repository SHA-256 reference."""

    match = _NAMED_IMAGE_DIGEST.fullmatch(value)
    if match is None or len(value) > 336:
        return False
    repository = match.group(1)
    components = repository.split("/")
    if any(not component for component in components):
        return False
    if len(components) > 1 and (
        "." in components[0]
        or ":" in components[0]
        or components[0] == "localhost"
    ):
        if _REGISTRY_COMPONENT.fullmatch(components[0]) is None:
            return False
        components = components[1:]
    return bool(components) and all(
        _REPOSITORY_COMPONENT.fullmatch(component) is not None
        for component in components
    )


def is_immutable_image_reference(value: str) -> bool:
    """Accept a submitted repository digest or a resolved local image ID."""

    return is_named_image_digest(value) or _LOCAL_IMAGE_ID.fullmatch(value) is not None


def _require_immutable_image_reference(value: str) -> None:
    if not is_immutable_image_reference(value):
        raise ValueError(
            "컨테이너 이미지는 repository@sha256:<64자리 소문자 16진수> "
            "또는 운영자가 확인한 sha256:<64자리 로컬 이미지 ID>여야 합니다."
        )


def _reject_duplicate_json_keys(
    pairs: Sequence[Tuple[str, Any]],
) -> Dict[str, Any]:
    value: Dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"중복 JSON 키: {key}")
        value[key] = item
    return value


def load_image_size_evidence(path: Path) -> ImageSizeEvidence:
    """Load bounded operator-only image measurements without following links."""

    try:
        parent = validate_operator_directory(
            path.parent,
            "이미지 크기 증거 상위 경로",
        )
        path = parent / path.name
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(str(path), flags)
    except InfrastructureUnavailable:
        raise
    except OSError as exc:
        raise InfrastructureUnavailable(
            f"이미지 크기 증거를 안전하게 열 수 없습니다: {exc}"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size > IMAGE_SIZE_EVIDENCE_MAX_BYTES
        ):
            raise InfrastructureUnavailable(
                "이미지 크기 증거는 운영자 소유 0600 단일 일반 파일이며 "
                f"{IMAGE_SIZE_EVIDENCE_MAX_BYTES}바이트 이하여야 합니다."
            )
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            payload = stream.read(IMAGE_SIZE_EVIDENCE_MAX_BYTES + 1)
    except InfrastructureUnavailable:
        raise
    except OSError as exc:
        raise InfrastructureUnavailable(
            f"이미지 크기 증거를 읽을 수 없습니다: {exc}"
        ) from exc
    finally:
        os.close(descriptor)
    try:
        value = json.loads(
            payload,
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (RecursionError, UnicodeError, ValueError) as exc:
        raise InfrastructureUnavailable(
            f"이미지 크기 증거 JSON을 해석할 수 없습니다: {exc}"
        ) from exc
    expected_keys = {
        "schema_version",
        "report_type",
        "submitted_digest",
        "selected_manifest_digest",
        "config_digest",
        "os",
        "architecture",
        "oci_compressed_layer_bytes",
        "rootfs_apparent_bytes",
        "oci_layer_measurement_method",
        "rootfs_measurement_method",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise InfrastructureUnavailable(
            "이미지 크기 증거의 필드 구성이 올바르지 않습니다."
        )
    if (
        isinstance(value["schema_version"], bool)
        or not isinstance(value["schema_version"], int)
        or value["schema_version"] != 1
        or value["report_type"] != "operator-image-size-evidence"
    ):
        raise InfrastructureUnavailable(
            "지원하지 않는 이미지 크기 증거 형식입니다."
        )
    integer_fields = (
        "oci_compressed_layer_bytes",
        "rootfs_apparent_bytes",
    )
    if any(
        isinstance(value[field], bool)
        or not isinstance(value[field], int)
        or value[field] < 0
        for field in integer_fields
    ):
        raise InfrastructureUnavailable(
            "이미지 크기 측정값은 0 이상의 정수 바이트여야 합니다."
        )
    string_fields = expected_keys - {
        "schema_version",
        *integer_fields,
    }
    if any(not isinstance(value[field], str) for field in string_fields):
        raise InfrastructureUnavailable(
            "이미지 크기 증거의 문자열 필드가 올바르지 않습니다."
        )
    evidence = ImageSizeEvidence(
        submitted_digest=value["submitted_digest"],
        selected_manifest_digest=value["selected_manifest_digest"],
        config_digest=value["config_digest"],
        operating_system=value["os"],
        architecture=value["architecture"],
        oci_compressed_layer_bytes=value["oci_compressed_layer_bytes"],
        rootfs_apparent_bytes=value["rootfs_apparent_bytes"],
        oci_layer_measurement_method=value["oci_layer_measurement_method"],
        rootfs_measurement_method=value["rootfs_measurement_method"],
        evidence_sha256=hashlib.sha256(payload).hexdigest(),
    )
    if (
        not is_named_image_digest(evidence.submitted_digest)
        or _LOCAL_IMAGE_ID.fullmatch(evidence.selected_manifest_digest) is None
        or _LOCAL_IMAGE_ID.fullmatch(evidence.config_digest) is None
        or evidence.operating_system != OFFICIAL_CONTAINER_OS
        or evidence.architecture != OFFICIAL_CONTAINER_ARCHITECTURE
        or evidence.oci_layer_measurement_method
        != OCI_LAYER_MEASUREMENT_METHOD
        or evidence.rootfs_measurement_method != ROOTFS_MEASUREMENT_METHOD
    ):
        raise InfrastructureUnavailable(
            "이미지 크기 증거의 다이제스트, 플랫폼 또는 측정 방식이 "
            "공식 전처리 규격과 다릅니다."
        )
    return evidence


def validate_image_size_evidence(
    evidence: ImageSizeEvidence,
    *,
    submitted_digest: str,
    local_image_id: str,
    operating_system: str,
    architecture: str,
) -> None:
    """Bind trusted measurements to the exact cached platform image and limit it."""

    if (
        _LOCAL_IMAGE_ID.fullmatch(evidence.selected_manifest_digest) is None
        or _LOCAL_IMAGE_ID.fullmatch(evidence.config_digest) is None
        or evidence.oci_layer_measurement_method
        != OCI_LAYER_MEASUREMENT_METHOD
        or evidence.rootfs_measurement_method != ROOTFS_MEASUREMENT_METHOD
        or re.fullmatch(r"[0-9a-f]{64}", evidence.evidence_sha256) is None
        or isinstance(evidence.oci_compressed_layer_bytes, bool)
        or not isinstance(evidence.oci_compressed_layer_bytes, int)
        or evidence.oci_compressed_layer_bytes < 0
        or isinstance(evidence.rootfs_apparent_bytes, bool)
        or not isinstance(evidence.rootfs_apparent_bytes, int)
        or evidence.rootfs_apparent_bytes < 0
    ):
        raise InfrastructureUnavailable(
            "이미지 크기 증거의 다이제스트, 측정값, 방식 또는 해시가 "
            "올바르지 않습니다."
        )
    if (
        evidence.submitted_digest != submitted_digest
        or local_image_id
        not in {
            evidence.selected_manifest_digest,
            evidence.config_digest,
        }
        or evidence.operating_system != operating_system
        or evidence.architecture != architecture
        or operating_system != OFFICIAL_CONTAINER_OS
        or architecture != OFFICIAL_CONTAINER_ARCHITECTURE
    ):
        raise InfrastructureUnavailable(
            "크기 증거와 제출 다이제스트·로컬 이미지 ID·플랫폼의 "
            "대응을 확인할 수 없습니다."
        )
    limits = PHASE_C_CANDIDATE_LIMITS
    if (
        evidence.oci_compressed_layer_bytes
        > limits.oci_compressed_image_bytes
    ):
        raise ImageLimitExceeded(
            f"선택한 {OFFICIAL_CONTAINER_PLATFORM} OCI 매니페스트의 "
            "압축 계층 합계가 "
            "공식 이미지 한도를 초과합니다."
        )
    if evidence.rootfs_apparent_bytes > limits.rootfs_apparent_bytes:
        raise ImageLimitExceeded(
            f"병합된 {OFFICIAL_CONTAINER_PLATFORM} rootfs의 겉보기 크기가 "
            "공식 이미지 한도를 초과합니다."
        )


def validate_image_configuration(value: Mapping[str, Any]) -> None:
    """Reject image-declared volumes that bypass operator write quotas."""

    try:
        config = value["Config"]
        if not isinstance(config, Mapping):
            raise TypeError("Config가 객체가 아님")
        volumes = config.get("Volumes")
    except (KeyError, TypeError) as exc:
        raise InfrastructureUnavailable(
            f"운영자가 이미지 설정을 해석할 수 없습니다: {exc}"
        ) from exc
    if volumes is not None and not isinstance(volumes, Mapping):
        raise InfrastructureUnavailable(
            "이미지 Config.Volumes 검사 결과가 올바르지 않습니다."
        )
    if volumes:
        raise DeclaredVolumeNotAllowed(
            "이미지의 VOLUME 선언은 제한 밖 쓰기 공간을 만들 수 있어 "
            "허용하지 않습니다."
        )


def validate_image_runtime_configuration(
    runtime_command: Sequence[str],
    image: str,
    *,
    platform: Optional[str] = None,
) -> None:
    """Inspect a cached image and enforce runtime configuration constraints."""

    validate_image_configuration(
        inspect_image_runtime_metadata(
            runtime_command,
            image,
            platform=platform,
        )
    )


def validate_host_core_dump_policy(
    runtime_command: Sequence[str],
    helper_name: str,
) -> None:
    """Inspect daemon core_pattern through the already journaled helper."""

    if not runtime_command:
        raise ValueError(
            "컨테이너 실행 명령은 비어 있을 수 없습니다."
        )
    completed, error = _run_control_command(
        [
            *runtime_command,
            "exec",
            helper_name,
            "python3",
            "-c",
            (
                "import pathlib,sys;"
                "sys.stdout.buffer.write(pathlib.Path("
                "'/proc/sys/kernel/core_pattern').read_bytes())"
            ),
        ],
        timeout_seconds=10,
        max_stdout_bytes=4096,
        max_stderr_bytes=4096,
    )
    if error is not None or completed is None or completed.returncode != 0:
        raise InfrastructureUnavailable(
            _control_failure_detail(
                "운영자가 Docker 호스트의 코어 덤프 정책을 확인할 수 없음",
                completed,
                error,
            )
        )
    pattern = completed.stdout.strip()
    if not pattern or b"\0" in pattern or b"\n" in pattern:
        raise InfrastructureUnavailable(
            "Docker 호스트의 core_pattern 검사 결과가 올바르지 않습니다."
        )
    if pattern.lstrip().startswith(b"|"):
        raise InfrastructureUnavailable(
            "Docker 호스트가 코어 덤프를 파이프 수집기로 전달합니다. "
            "파이프 수집을 비활성화한 평가 호스트가 필요합니다."
        )


def inspect_image_runtime_metadata(
    runtime_command: Sequence[str],
    image: str,
    *,
    platform: Optional[str] = None,
) -> Mapping[str, Any]:
    """Return only bounded image fields needed by the official runner."""

    if platform is not None and re.fullmatch(
        r"[a-z0-9][a-z0-9_.-]*/[a-z0-9][a-z0-9_.-]*",
        platform,
    ) is None:
        raise ValueError("이미지 플랫폼 형식이 올바르지 않습니다.")
    template = "\n".join(
        (
            "{{json .Id}}",
            "{{json .RepoDigests}}",
            "{{json .Os}}",
            "{{json .Architecture}}",
            '{{if (index .Config "Volumes")}}true{{else}}false{{end}}',
        )
    )
    arguments = [*runtime_command, "image", "inspect"]
    if platform is not None:
        arguments.extend(["--platform", platform])
    arguments.extend(["--format", template, image])
    completed, error = _run_control_command(
        arguments,
        timeout_seconds=15,
        max_stdout_bytes=1024 * 1024,
        max_stderr_bytes=64 * 1024,
    )
    if error is not None or completed is None or completed.returncode != 0:
        raise InfrastructureUnavailable(
            _control_failure_detail(
                "운영자가 이미지 설정을 확인할 수 없음",
                completed,
                error,
            )
        )
    try:
        lines = completed.stdout.decode("utf-8").splitlines()
        if len(lines) != 5:
            raise ValueError("inspect 결과 줄 수 오류")
        (
            local_id,
            repository_digests,
            operating_system,
            architecture,
            has_declared_volumes,
        ) = (json.loads(line) for line in lines)
        if not isinstance(has_declared_volumes, bool):
            raise TypeError("VOLUME 존재 여부가 불리언이 아님")
        if platform is not None:
            expected_os, expected_architecture = platform.split("/", 1)
            if operating_system == "":
                operating_system = expected_os
            if architecture == "":
                architecture = expected_architecture
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise InfrastructureUnavailable(
            f"운영자가 이미지 설정을 해석할 수 없습니다: {exc}"
        ) from exc
    return {
        "Id": local_id,
        "RepoDigests": repository_digests,
        "Os": operating_system,
        "Architecture": architecture,
        "Config": {
            "Volumes": {"<declared>": {}} if has_declared_volumes else None
        },
    }


def _random_docker_name(prefix: str) -> str:
    return f"{prefix}-{os.getpid()}-{secrets.token_hex(8)}"


def build_isolated_container_command(
    runtime_command: Sequence[str],
    *,
    image: str,
    input_path: Path,
    output_volume: str,
    container_name: str,
    attempt_token: str,
    tier: str,
    platform: Optional[str] = None,
    cidfile: Optional[Path] = None,
    termination_grace_seconds: float = DEFAULT_TERMINATION_GRACE_SECONDS,
) -> List[str]:
    """Build one Docker-compatible, resource-bounded tier invocation."""

    if not runtime_command:
        raise ValueError(
            "컨테이너 실행 명령은 비어 있을 수 없습니다."
        )
    _require_immutable_image_reference(image)
    if not output_volume or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,127}", output_volume):
        raise ValueError("출력 볼륨 이름이 올바르지 않습니다.")
    if not container_name or not re.fullmatch(
        r"[a-z0-9][a-z0-9_.-]{0,127}", container_name
    ):
        raise ValueError("참가자 컨테이너 이름이 올바르지 않습니다.")
    if re.fullmatch(r"[0-9a-f]{32}", attempt_token) is None:
        raise ValueError("실행 식별값은 32자리 소문자 16진수여야 합니다.")
    if tier not in TIERS:
        raise ValueError(f"알 수 없는 실행 등급: {tier}")
    if termination_grace_seconds < 0:
        raise ValueError("termination_grace_seconds는 0 이상이어야 합니다.")
    if platform is not None and re.fullmatch(
        r"[a-z0-9][a-z0-9_.-]*/[a-z0-9][a-z0-9_.-]*",
        platform,
    ) is None:
        raise ValueError("컨테이너 플랫폼 형식이 올바르지 않습니다.")

    limits = PHASE_C_CANDIDATE_LIMITS
    command = [
        *runtime_command,
        "run",
        "--pull",
        "never",
        "--log-driver",
        "none",
        "--stop-timeout",
        str(int(termination_grace_seconds)),
        "--stop-signal",
        "SIGTERM",
        "--no-healthcheck",
        "--name",
        container_name,
        "--label",
        f"{ATTEMPT_LABEL}={attempt_token}",
    ]
    if platform is not None:
        command.extend(["--platform", platform])
    if cidfile is not None:
        command.extend(["--cidfile", str(cidfile.resolve())])
    command.extend(
        [
            "--network",
            "none",
            "--ipc",
            "none",
            "--cgroupns",
            "private",
            "--ulimit",
            "core=0:0",
            "--read-only",
            "--user",
            CONTAINER_USER,
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--cpus",
            str(limits.cpu_cores),
            "--memory",
            _docker_size(limits.memory_bytes),
            "--memory-swap",
            _docker_size(limits.memory_swap_total_bytes),
            "--pids-limit",
            str(limits.processes_and_threads_total),
            "--tmpfs",
            (
                f"{CONTAINER_TEMPORARY_DIRECTORY}:rw,noexec,nosuid,"
                f"size={_docker_size(limits.temporary_storage_bytes)}"
            ),
            "--mount",
            (
                f"type=bind,src={input_path.resolve()},"
                f"dst={CONTAINER_INPUT_PATH},readonly"
            ),
            "--mount",
            (
                f"type=volume,src={output_volume},"
                f"dst={CONTAINER_OUTPUT_DIRECTORY},volume-nocopy"
            ),
            image,
            "--input",
            CONTAINER_INPUT_PATH,
            "--tier",
            tier,
            "--output",
            CONTAINER_OUTPUT_PATH,
        ]
    )
    return command


def _run_prepared_router_once(
    arguments: Sequence[str],
    *,
    inputs: InputBatch,
    policy: RoutingPolicy,
    output_path: Path,
    tier: str,
    timeout_seconds: float,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    max_log_bytes: int = DEFAULT_MAX_LOG_BYTES,
    max_log_emitted_bytes: int = DEFAULT_MAX_LOG_EMITTED_BYTES,
    termination_grace_seconds: float = DEFAULT_TERMINATION_GRACE_SECONDS,
    monitor_output_path: bool = True,
    post_exit: Optional[Callable[[], Optional[AttemptResult]]] = None,
) -> AttemptResult:
    """Run prepared arguments once and classify participant-visible failures."""

    if tier not in TIERS:
        raise ValueError(f"알 수 없는 실행 등급: {tier}")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds는 0보다 커야 합니다.")
    if termination_grace_seconds < 0:
        raise ValueError("termination_grace_seconds는 0 이상이어야 합니다.")
    if max_output_bytes < 0:
        raise ValueError("max_output_bytes는 0 이상이어야 합니다.")
    if max_log_bytes < 0:
        raise ValueError("max_log_bytes는 0 이상이어야 합니다.")
    if max_log_emitted_bytes < max_log_bytes:
        raise ValueError(
            "max_log_emitted_bytes는 max_log_bytes 이상이어야 합니다."
        )

    try:
        output_path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        return AttemptResult(
            AttemptKind.INFRASTRUCTURE_FAILURE,
            f"운영자가 이전 출력 파일을 정리할 수 없음: {exc}",
        )

    measurement_started_ns = time.perf_counter_ns()

    def measured(result: AttemptResult) -> AttemptResult:
        return replace(
            result,
            measurement_elapsed_ns=max(
                0,
                time.perf_counter_ns() - measurement_started_ns,
            ),
        )

    try:
        process = subprocess.Popen(
            arguments,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        return measured(
            AttemptResult(
                AttemptKind.INFRASTRUCTURE_FAILURE,
                f"운영자 실행 환경에서 프로세스를 시작할 수 없음: {exc}",
            )
        )

    selector = selectors.DefaultSelector()
    captures: Dict[int, _BoundedPipeCapture] = {}
    assert process.stdout is not None
    assert process.stderr is not None
    try:
        for stream in (process.stdout, process.stderr):
            descriptor = stream.fileno()
            os.set_blocking(descriptor, False)
            captures[descriptor] = _BoundedPipeCapture(
                max_log_bytes,
                max_log_emitted_bytes,
            )
            selector.register(descriptor, selectors.EVENT_READ)
    except (OSError, ValueError) as exc:
        _terminate_process_group(process, termination_grace_seconds)
        selector.close()
        process.stdout.close()
        process.stderr.close()
        return measured(
            AttemptResult(
                AttemptKind.INFRASTRUCTURE_FAILURE,
                f"운영자가 실행 로그 파이프를 준비할 수 없음: {exc}",
                returncode=process.returncode,
                operator_terminated=True,
            )
        )

    deadline = time.monotonic() + timeout_seconds
    failure_detail = None
    failure_kind = AttemptKind.EXECUTION_FAILURE
    timed_out = False
    while process.poll() is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            failure_detail = (
                f"{timeout_seconds:g}초 제한 시간 초과 "
                f"(SIGTERM 후 {termination_grace_seconds:g}초 뒤 SIGKILL)"
            )
            timed_out = True
            break
        _drain_ready_pipes(selector, captures, min(0.05, remaining))
        if any(capture.emission_exceeded for capture in captures.values()):
            failure_detail = (
                "표준 출력 또는 표준 오류가 각각 "
                f"{max_log_emitted_bytes}바이트인 방출 한도를 넘었습니다."
            )
            break
        if monitor_output_path:
            try:
                output_size = output_path.stat().st_size
            except FileNotFoundError:
                continue
            except OSError as exc:
                failure_detail = f"운영자가 출력 파일을 검사할 수 없음: {exc}"
                failure_kind = AttemptKind.INFRASTRUCTURE_FAILURE
                break
            if output_size > max_output_bytes:
                failure_detail = (
                    f"출력 파일이 {max_output_bytes}바이트 한도를 넘었습니다."
                )
                break

    if failure_detail is not None:
        _terminate_process_group(process, termination_grace_seconds)
        stdout, stderr = _finalize_pipe_capture(selector, process, captures)
        return measured(
            AttemptResult(
                failure_kind,
                failure_detail,
                returncode=process.returncode,
                stdout=stdout,
                stderr=stderr,
                timed_out=timed_out,
                operator_terminated=True,
            )
        )

    stdout, stderr = _finalize_pipe_capture(selector, process, captures)
    if any(capture.emission_exceeded for capture in captures.values()):
        return measured(
            AttemptResult(
                AttemptKind.EXECUTION_FAILURE,
                (
                    "표준 출력 또는 표준 오류가 각각 "
                    f"{max_log_emitted_bytes}바이트인 방출 한도를 넘었습니다."
                ),
                returncode=process.returncode,
                stdout=stdout,
                stderr=stderr,
            )
        )
    if process.returncode != 0:
        return measured(
            AttemptResult(
                AttemptKind.EXECUTION_FAILURE,
                f"비정상 종료 코드: {process.returncode}",
                returncode=process.returncode,
                stdout=stdout,
                stderr=stderr,
            )
        )
    if post_exit is not None:
        post_result = post_exit()
        if post_result is not None:
            return measured(
                replace(
                    post_result,
                    returncode=process.returncode,
                    stdout=stdout,
                    stderr=stderr,
                )
            )
    if output_path.is_symlink() or not output_path.is_file():
        return measured(
            AttemptResult(
                AttemptKind.EXECUTION_FAILURE,
                "출력 파일이 생성되지 않았습니다.",
                returncode=process.returncode,
                stdout=stdout,
                stderr=stderr,
            )
        )
    try:
        output_size = output_path.stat().st_size
    except OSError as exc:
        return measured(
            AttemptResult(
                AttemptKind.INFRASTRUCTURE_FAILURE,
                f"운영자가 출력 파일을 검사할 수 없음: {exc}",
                returncode=process.returncode,
                stdout=stdout,
                stderr=stderr,
            )
        )
    if output_size > max_output_bytes:
        return measured(
            AttemptResult(
                AttemptKind.EXECUTION_FAILURE,
                f"출력 파일이 {max_output_bytes}바이트 한도를 넘었습니다.",
                returncode=process.returncode,
                stdout=stdout,
                stderr=stderr,
                output_bytes=output_size,
            )
        )
    try:
        output_sha256 = hashlib.sha256(output_path.read_bytes()).hexdigest()
    except OSError as exc:
        return measured(
            AttemptResult(
                AttemptKind.INFRASTRUCTURE_FAILURE,
                f"운영자가 출력 파일의 SHA-256을 계산할 수 없음: {exc}",
                returncode=process.returncode,
                stdout=stdout,
                stderr=stderr,
                output_bytes=output_size,
            )
        )
    try:
        submission = load_submission(output_path)
        validate_runtime_submission(inputs, submission, policy, tier)
    except (OSError, ProtocolError) as exc:
        return measured(
            AttemptResult(
                AttemptKind.EXECUTION_FAILURE,
                f"출력 검증 실패: {exc}",
                returncode=process.returncode,
                stdout=stdout,
                stderr=stderr,
                output_bytes=output_size,
                output_sha256=output_sha256,
            )
        )
    return measured(
        AttemptResult(
            AttemptKind.VALID,
            "유효한 제출 파일",
            submission=submission,
            returncode=process.returncode,
            stdout=stdout,
            stderr=stderr,
            output_bytes=output_size,
            output_sha256=output_sha256,
        )
    )


def run_router_once(
    command: Sequence[str],
    *,
    inputs: InputBatch,
    policy: RoutingPolicy,
    input_path: Path,
    output_path: Path,
    tier: str,
    timeout_seconds: float = PHASE_C_CANDIDATE_LIMITS.wall_time_seconds,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    max_log_bytes: int = DEFAULT_MAX_LOG_BYTES,
    max_log_emitted_bytes: int = DEFAULT_MAX_LOG_EMITTED_BYTES,
    termination_grace_seconds: float = DEFAULT_TERMINATION_GRACE_SECONDS,
) -> AttemptResult:
    """Run the fixed host interface once and classify participant-visible failures."""

    arguments = [
        *command,
        "--input",
        str(input_path),
        "--tier",
        tier,
        "--output",
        str(output_path),
    ]
    return _run_prepared_router_once(
        arguments,
        inputs=inputs,
        policy=policy,
        output_path=output_path,
        tier=tier,
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
        max_log_bytes=max_log_bytes,
        max_log_emitted_bytes=max_log_emitted_bytes,
        termination_grace_seconds=termination_grace_seconds,
    )


def _run_control_command(
    arguments: Sequence[str],
    *,
    timeout_seconds: float = 15.0,
    max_stdout_bytes: int = 1024 * 1024,
    max_stderr_bytes: int = 64 * 1024,
) -> Tuple[Optional[subprocess.CompletedProcess[bytes]], Optional[str]]:
    if timeout_seconds <= 0:
        return None, "제한 시간은 0보다 커야 합니다."
    if max_stdout_bytes < 0 or max_stderr_bytes < 0:
        return None, "제어 명령 출력 한도는 0 이상이어야 합니다."
    try:
        process = subprocess.Popen(
            arguments,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        return None, str(exc)
    assert process.stdout is not None
    assert process.stderr is not None
    selector = selectors.DefaultSelector()
    captures: Dict[int, _BoundedPipeCapture] = {}
    try:
        for stream, maximum in (
            (process.stdout, max_stdout_bytes),
            (process.stderr, max_stderr_bytes),
        ):
            descriptor = stream.fileno()
            os.set_blocking(descriptor, False)
            captures[descriptor] = _BoundedPipeCapture(maximum, maximum)
            selector.register(descriptor, selectors.EVENT_READ)
    except (OSError, ValueError) as exc:
        _terminate_process_group(process, 0.2)
        selector.close()
        process.stdout.close()
        process.stderr.close()
        return None, f"제어 명령 로그 파이프 오류: {exc}"

    deadline = time.monotonic() + timeout_seconds
    failure = None
    while process.poll() is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            failure = f"제어 명령이 {timeout_seconds:g}초 제한 시간을 넘었습니다."
            break
        _drain_ready_pipes(selector, captures, min(0.05, remaining))
        if any(capture.emission_exceeded for capture in captures.values()):
            failure = (
                "제어 명령 출력이 stdout "
                f"{max_stdout_bytes}바이트 또는 stderr "
                f"{max_stderr_bytes}바이트 한도를 넘었습니다."
            )
            break
    if failure is not None:
        _terminate_process_group(process, 0.2)
    final_drain_rounds = (
        (max_stdout_bytes + max_stderr_bytes) // (64 * 1024) + 8
    )
    for _ in range(final_drain_rounds):
        if _drain_ready_pipes(selector, captures, 0) == 0:
            break
    stdout = captures[process.stdout.fileno()].raw()
    stderr = captures[process.stderr.fileno()].raw()
    exceeded = any(capture.emission_exceeded for capture in captures.values())
    selector.close()
    process.stdout.close()
    process.stderr.close()
    if failure is not None or exceeded:
        return None, failure or "제어 명령 출력 한도를 넘었습니다."
    return (
        subprocess.CompletedProcess(
            args=arguments,
            returncode=process.returncode,
            stdout=stdout,
            stderr=stderr,
        ),
        None,
    )


def _control_failure_detail(
    label: str,
    completed: Optional[subprocess.CompletedProcess[bytes]],
    error: Optional[str],
) -> str:
    if error is not None:
        return f"{label}: {error}"
    assert completed is not None
    detail = _bounded_log(completed.stderr, 2048).strip()
    return f"{label} (종료 코드 {completed.returncode}): {detail}"


def inspect_runtime_daemon_id(runtime_command: Sequence[str]) -> str:
    """Read a bounded Docker Engine ID without mutating daemon state."""

    if not runtime_command:
        raise ValueError("컨테이너 실행 명령은 비어 있을 수 없습니다.")
    completed, error = _run_control_command(
        [
            *runtime_command,
            "info",
            "--format",
            "{{json .ID}}",
        ],
        timeout_seconds=10,
        max_stdout_bytes=1024,
        max_stderr_bytes=4096,
    )
    if error is not None or completed is None or completed.returncode != 0:
        raise InfrastructureUnavailable(
            _control_failure_detail(
                "Docker Engine ID를 확인할 수 없음",
                completed,
                error,
            )
        )
    try:
        daemon_id = json.loads(completed.stdout)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InfrastructureUnavailable(
            f"Docker Engine ID 응답을 해석할 수 없습니다: {exc}"
        ) from exc
    if (
        not isinstance(daemon_id, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}", daemon_id)
        is None
    ):
        raise InfrastructureUnavailable(
            "Docker Engine ID 형식이 올바르지 않습니다."
        )
    return daemon_id


def _runtime_daemon_identity_error(
    runtime_command: Sequence[str],
    expected_daemon_id: str,
) -> Optional[str]:
    try:
        current_daemon_id = inspect_runtime_daemon_id(runtime_command)
    except InfrastructureUnavailable as exc:
        return str(exc)
    if current_daemon_id != expected_daemon_id:
        return (
            "실행 중 Docker Engine ID가 변경되어 자원을 안전하게 "
            "정리할 수 없습니다."
        )
    return None


def _control_target_absent(
    completed: Optional[subprocess.CompletedProcess[bytes]],
) -> bool:
    if completed is None or completed.returncode == 0:
        return False
    detail = completed.stderr.decode("utf-8", errors="replace").lower()
    return (
        "no such container" in detail
        or "no such volume" in detail
        or "no such object" in detail
    )


def _inspect_attempt_label(
    runtime_command: Sequence[str],
    *,
    resource: str,
    reference: str,
) -> Tuple[Optional[str], Optional[str], bool]:
    completed = None
    error = None
    for delay in (0.0, 0.1, 0.3, 0.6):
        if delay:
            time.sleep(delay)
        completed, error = _run_control_command(
            [
                *runtime_command,
                resource,
                "inspect",
                "--format",
                f'{{{{index .Labels "{ATTEMPT_LABEL}"}}}}'
                if resource == "volume"
                else f'{{{{index .Config.Labels "{ATTEMPT_LABEL}"}}}}',
                reference,
            ],
            timeout_seconds=5,
            max_stdout_bytes=1024,
            max_stderr_bytes=4096,
        )
        if not _control_target_absent(completed):
            break
    if _control_target_absent(completed):
        return None, None, True
    if error is not None or completed is None or completed.returncode != 0:
        return (
            None,
            _control_failure_detail(
                f"운영자 {resource} 실행 식별값을 확인할 수 없음",
                completed,
                error,
            ),
            False,
        )
    try:
        label = completed.stdout.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        return None, f"운영자 {resource} 실행 식별값이 ASCII가 아님: {exc}", False
    if re.fullmatch(r"[0-9a-f]{32}", label) is None:
        return None, f"운영자 {resource} 실행 식별값 형식이 올바르지 않음", False
    return label, None, False


def _create_output_volume(
    runtime_command: Sequence[str],
    volume_name: str,
    maximum: int,
    attempt_token: str,
) -> Optional[str]:
    options = (
        f"size={maximum},nr_inodes={OUTPUT_VOLUME_INODE_LIMIT},"
        "uid=65532,gid=65532,mode=0770,noexec,nosuid,nodev"
    )
    completed, error = _run_control_command(
        [
            *runtime_command,
            "volume",
            "create",
            "--driver",
            "local",
            "--opt",
            "type=tmpfs",
            "--opt",
            "device=tmpfs",
            "--opt",
            f"o={options}",
            "--label",
            f"{ATTEMPT_LABEL}={attempt_token}",
            volume_name,
        ]
    )
    if error is not None or completed is None or completed.returncode != 0:
        return _control_failure_detail(
            "운영자 출력 제한 볼륨을 만들 수 없음",
            completed,
            error,
        )
    label, label_error, absent = _inspect_attempt_label(
        runtime_command,
        resource="volume",
        reference=volume_name,
    )
    if label_error is not None or absent or label != attempt_token:
        return label_error or (
            "운영자 출력 제한 볼륨의 실행 식별값이 일치하지 않습니다."
        )
    return None


def _start_output_helper(
    runtime_command: Sequence[str],
    *,
    helper_name: str,
    volume_name: str,
    result_directory: Path,
    attempt_token: str,
) -> Optional[str]:
    command = [
        *runtime_command,
        "run",
        "--detach",
        "--rm",
        "--pull",
        "never",
        "--log-driver",
        "none",
        "--name",
        helper_name,
        "--label",
        f"{ATTEMPT_LABEL}={attempt_token}",
        "--network",
        "none",
        "--ipc",
        "none",
        "--ulimit",
        "core=0:0",
        "--read-only",
        "--user",
        CONTAINER_USER,
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--memory",
        "64m",
        "--memory-swap",
        "64m",
        "--pids-limit",
        "8",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=8m",
        "--mount",
        (
            f"type=volume,src={volume_name},"
            f"dst={CONTAINER_OUTPUT_DIRECTORY},readonly,volume-nocopy"
        ),
        "--mount",
        (
            f"type=bind,src={result_directory.resolve()},"
            f"dst={OPERATOR_RESULT_DIRECTORY}"
        ),
        OPERATOR_HELPER_IMAGE,
        "python3",
        OPERATOR_HELPER_SCRIPT,
        "hold",
    ]
    completed, error = _run_control_command(command)
    if error is not None or completed is None or completed.returncode != 0:
        return _control_failure_detail(
            "운영자 출력 도우미를 시작할 수 없음",
            completed,
            error,
        )
    label, label_error, absent = _inspect_attempt_label(
        runtime_command,
        resource="container",
        reference=helper_name,
    )
    if label_error is not None or absent or label != attempt_token:
        return label_error or (
            "운영자 출력 도우미의 실행 식별값이 일치하지 않습니다."
        )
    return None


def _extract_output_from_helper(
    runtime_command: Sequence[str],
    *,
    helper_name: str,
    output_path: Path,
    maximum: int,
) -> Optional[AttemptResult]:
    completed, error = _run_control_command(
        [
            *runtime_command,
            "exec",
            helper_name,
            "python3",
            OPERATOR_HELPER_SCRIPT,
            "extract",
            "--source",
            CONTAINER_OUTPUT_DIRECTORY,
            "--destination",
            f"{OPERATOR_RESULT_DIRECTORY}/{output_path.name}",
            "--maximum",
            str(maximum),
        ]
    )
    if error is not None or completed is None:
        return AttemptResult(
            AttemptKind.INFRASTRUCTURE_FAILURE,
            _control_failure_detail(
                "운영자가 제한 출력 공간에서 결과를 회수할 수 없음",
                completed,
                error,
            ),
        )
    detail = _bounded_log(completed.stderr, 2048).strip()
    if completed.returncode == 0:
        return None
    if completed.returncode == 2:
        return AttemptResult(
            AttemptKind.EXECUTION_FAILURE,
            f"출력 공간 검증 실패: {detail}",
        )
    return AttemptResult(
        AttemptKind.INFRASTRUCTURE_FAILURE,
        (
            "운영자가 제한 출력 공간에서 결과를 회수할 수 없음 "
            f"(종료 코드 {completed.returncode}): {detail}"
        ),
    )


def _remove_output_workspace(
    runtime_command: Sequence[str],
    *,
    helper_name: str,
    volume_name: str,
    helper_started: bool,
    volume_created: bool,
    attempt_token: str,
) -> Tuple[str, ...]:
    errors = []
    if helper_started:
        label, label_error, absent = _inspect_attempt_label(
            runtime_command,
            resource="container",
            reference=helper_name,
        )
        if label_error is not None:
            errors.append(label_error)
        elif not absent and label != attempt_token:
            errors.append(
                "운영자 출력 도우미 이름 충돌을 감지해 제거하지 않았습니다."
            )
        elif not absent:
            completed, error = _run_control_command(
                [*runtime_command, "rm", "--force", helper_name]
            )
            if (
                error is not None
                or completed is None
                or (
                    completed.returncode != 0
                    and not _control_target_absent(completed)
                )
            ):
                errors.append(
                    _control_failure_detail(
                        "운영자 출력 도우미를 제거할 수 없음",
                        completed,
                        error,
                    )
                )
    if volume_created:
        label, label_error, absent = _inspect_attempt_label(
            runtime_command,
            resource="volume",
            reference=volume_name,
        )
        if label_error is not None:
            errors.append(label_error)
        elif not absent and label != attempt_token:
            errors.append(
                "운영자 출력 제한 볼륨 이름 충돌을 감지해 제거하지 않았습니다."
            )
        elif not absent:
            completed, error = _run_control_command(
                [*runtime_command, "volume", "rm", "--force", volume_name]
            )
            if (
                error is not None
                or completed is None
                or (
                    completed.returncode != 0
                    and not _control_target_absent(completed)
                )
            ):
                errors.append(
                    _control_failure_detail(
                        "운영자 출력 제한 볼륨을 제거할 수 없음",
                        completed,
                        error,
                    )
                )
    return tuple(errors)


@dataclass
class _OutputCleanupState:
    volume_created: bool = False
    helper_start_attempted: bool = False
    errors: Tuple[str, ...] = ()


@contextmanager
def _managed_output_cleanup(
    runtime_command: Sequence[str],
    *,
    helper_name: str,
    volume_name: str,
    attempt_token: str,
    expected_daemon_id: str,
) -> Iterator[_OutputCleanupState]:
    state = _OutputCleanupState()
    try:
        yield state
    finally:
        if not state.helper_start_attempted and not state.volume_created:
            state.errors = ()
        else:
            identity_error = _runtime_daemon_identity_error(
                runtime_command,
                expected_daemon_id,
            )
            if identity_error is not None:
                state.errors = (identity_error,)
            else:
                state.errors = _remove_output_workspace(
                    runtime_command,
                    helper_name=helper_name,
                    volume_name=volume_name,
                    helper_started=state.helper_start_attempted,
                    volume_created=state.volume_created,
                    attempt_token=attempt_token,
                )


@dataclass(frozen=True)
class _ContainerStateSnapshot:
    container_id: str
    attempt_token: str
    status: str
    exit_code: int
    runtime_error: bool
    oom_killed: bool
    started_at: str


def _inspect_participant_container(
    runtime_command: Sequence[str],
    reference: str,
) -> Tuple[Optional[_ContainerStateSnapshot], Optional[str], bool]:
    template = "\n".join(
        (
            "{{json .State.Status}}",
            "{{json .State.ExitCode}}",
            "{{if .State.Error}}true{{else}}false{{end}}",
            "{{json .State.OOMKilled}}",
            "{{json .State.StartedAt}}",
            f'{{{{json (index .Config.Labels "{ATTEMPT_LABEL}")}}}}',
            "{{json .Id}}",
        )
    )
    completed = None
    error = None
    for delay in (0.0, 0.1, 0.3, 0.6):
        if delay:
            time.sleep(delay)
        completed, error = _run_control_command(
            [
                *runtime_command,
                "container",
                "inspect",
                "--format",
                template,
                reference,
            ],
            timeout_seconds=5,
            max_stdout_bytes=16 * 1024,
            max_stderr_bytes=16 * 1024,
        )
        if not _control_target_absent(completed):
            break
    if _control_target_absent(completed):
        return None, None, True
    if error is not None or completed is None or completed.returncode != 0:
        return (
            None,
            _control_failure_detail(
                "참가자 컨테이너 상태를 확인할 수 없음",
                completed,
                error,
            ),
            False,
        )
    if (
        not isinstance(completed.stdout, bytes)
        or len(completed.stdout) > 16 * 1024
    ):
        return (
            None,
            "참가자 컨테이너 상태 응답 크기가 올바르지 않습니다.",
            False,
        )
    try:
        lines = completed.stdout.decode("utf-8").splitlines()
        if len(lines) != 7:
            raise ValueError("inspect 응답 줄 수 오류")
        (
            status,
            exit_code,
            runtime_error,
            oom_killed,
            started_at,
            attempt_token,
            container_id,
        ) = (json.loads(line) for line in lines)
        if (
            re.fullmatch(r"[0-9a-f]{64}", container_id) is None
            or not isinstance(attempt_token, str)
            or re.fullmatch(r"[0-9a-f]{32}", attempt_token) is None
            or not isinstance(status, str)
            or not isinstance(exit_code, int)
            or isinstance(exit_code, bool)
            or not isinstance(runtime_error, bool)
            or not isinstance(oom_killed, bool)
            or not isinstance(started_at, str)
        ):
            raise TypeError("inspect 필드 형식 오류")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return (
            None,
            f"참가자 컨테이너 상태를 해석할 수 없음: {exc}",
            False,
        )
    return (
        _ContainerStateSnapshot(
            container_id=container_id,
            attempt_token=attempt_token,
            status=status,
            exit_code=exit_code,
            runtime_error=runtime_error,
            oom_killed=oom_killed,
            started_at=started_at,
        ),
        None,
        False,
    )


def _stop_participant_container(
    runtime_command: Sequence[str],
    container_id: str,
    termination_grace_seconds: float,
) -> Optional[str]:
    completed, error = _run_control_command(
        [
            *runtime_command,
            "stop",
            "--timeout",
            str(int(termination_grace_seconds)),
            container_id,
        ],
        timeout_seconds=termination_grace_seconds + 5,
    )
    if error is None and completed is not None and completed.returncode == 0:
        return None
    return _control_failure_detail(
        "참가자 컨테이너를 종료할 수 없음",
        completed,
        error,
    )


def _remove_participant_container(
    runtime_command: Sequence[str],
    container_id: str,
) -> Optional[str]:
    completed, error = _run_control_command(
        [*runtime_command, "rm", "--force", container_id],
        timeout_seconds=5,
    )
    if error is None and completed is not None and completed.returncode == 0:
        return None
    return _control_failure_detail(
        "참가자 컨테이너를 제거할 수 없음",
        completed,
        error,
    )


def _infrastructure_result(
    result: AttemptResult,
    detail: str,
) -> AttemptResult:
    return replace(
        result,
        kind=AttemptKind.INFRASTRUCTURE_FAILURE,
        detail=f"{result.detail}; {detail}" if result.detail else detail,
        submission=None,
    )


def _reconcile_and_remove_participant_container(
    runtime_command: Sequence[str],
    *,
    container_name: str,
    attempt_token: str,
    result: AttemptResult,
    termination_grace_seconds: float,
    observe_owned: Optional[Callable[[], None]] = None,
) -> AttemptResult:
    """Classify Docker state and remove only the container bearing our label."""

    snapshot, inspect_error, absent = _inspect_participant_container(
        runtime_command,
        container_name,
    )
    if snapshot is None:
        reconciled = _infrastructure_result(
            result,
            inspect_error or "참가자 컨테이너가 생성되지 않았습니다.",
        )
        if result.operator_terminated or not absent:
            reconciled = replace(reconciled, cleanup_pending=True)
        return reconciled
    if snapshot.attempt_token != attempt_token:
        return replace(
            _infrastructure_result(
                result,
                "참가자 컨테이너 이름 충돌을 감지해 다른 컨테이너를 건드리지 않았습니다.",
            ),
            cleanup_pending=True,
        )

    reconciled = result
    if observe_owned is not None:
        try:
            observe_owned()
        except InfrastructureUnavailable as exc:
            reconciled = replace(
                _infrastructure_result(reconciled, str(exc)),
                cleanup_pending=True,
            )
    status_after_cli = snapshot.status
    container_absent = False
    safe_to_remove = True
    try:
        if snapshot.status in {"running", "restarting", "paused"}:
            stop_error = _stop_participant_container(
                runtime_command,
                snapshot.container_id,
                termination_grace_seconds,
            )
            if stop_error is not None:
                reconciled = _infrastructure_result(reconciled, stop_error)
            else:
                (
                    stopped,
                    stopped_error,
                    stopped_absent,
                ) = _inspect_participant_container(
                    runtime_command,
                    snapshot.container_id,
                )
                if stopped is None:
                    if stopped_absent:
                        container_absent = True
                    else:
                        reconciled = replace(
                            _infrastructure_result(
                                reconciled,
                                stopped_error
                                or "종료한 참가자 컨테이너 상태를 확인할 수 없습니다.",
                            ),
                            cleanup_pending=True,
                        )
                elif stopped.attempt_token != attempt_token:
                    safe_to_remove = False
                    reconciled = replace(
                        _infrastructure_result(
                            reconciled,
                            "종료 후 참가자 컨테이너 실행 식별값이 달라졌습니다.",
                        ),
                        cleanup_pending=True,
                    )
                else:
                    snapshot = stopped

        if (
            not container_absent
            and reconciled.kind is not AttemptKind.INFRASTRUCTURE_FAILURE
        ):
            never_started = (
                not snapshot.started_at
                or snapshot.started_at.startswith("0001-01-01T00:00:00")
            )
            if result.returncode in {126, 127}:
                # Docker reserves these codes for an image command that cannot
                # be invoked or found. They are participant execution errors
                # even when no process reached StartedAt.
                pass
            elif never_started:
                reconciled = _infrastructure_result(
                    reconciled,
                    "컨테이너 프로세스가 시작되었다는 상태 증거가 없습니다.",
                )
            elif snapshot.runtime_error:
                reconciled = _infrastructure_result(
                    reconciled,
                    "컨테이너 런타임 오류 상태가 기록되었습니다.",
                )
            elif snapshot.oom_killed:
                reconciled = replace(
                    reconciled,
                    kind=AttemptKind.EXECUTION_FAILURE,
                    detail="메모리 한도를 넘어 컨테이너가 종료됐습니다.",
                    submission=None,
                )
            elif result.operator_terminated:
                if snapshot.status != "exited":
                    reconciled = _infrastructure_result(
                        reconciled,
                        f"강제 종료 후 컨테이너 상태가 {snapshot.status!r}입니다.",
                    )
            elif status_after_cli != "exited":
                reconciled = _infrastructure_result(
                    reconciled,
                    f"Docker CLI 종료 후 컨테이너 상태가 {status_after_cli!r}입니다.",
                )
            elif result.returncode != snapshot.exit_code:
                reconciled = _infrastructure_result(
                    reconciled,
                    "Docker CLI 종료 코드와 컨테이너 종료 코드가 일치하지 않습니다.",
                )
    finally:
        if not container_absent and safe_to_remove:
            removal_error = _remove_participant_container(
                runtime_command,
                snapshot.container_id,
            )
            if removal_error is not None:
                reconciled = replace(
                    _infrastructure_result(reconciled, removal_error),
                    cleanup_pending=True,
                )
    return reconciled


def _best_effort_remove_participant_container(
    runtime_command: Sequence[str],
    *,
    container_name: str,
    attempt_token: str,
    termination_grace_seconds: float,
) -> None:
    """Remove an owned container when an unexpected operator exception escapes."""

    snapshot, _error, _absent = _inspect_participant_container(
        runtime_command,
        container_name,
    )
    if snapshot is None or snapshot.attempt_token != attempt_token:
        return
    if snapshot.status in {"running", "restarting", "paused"}:
        _stop_participant_container(
            runtime_command,
            snapshot.container_id,
            termination_grace_seconds,
        )
    _remove_participant_container(runtime_command, snapshot.container_id)


_RESOURCE_ROLES = ("participant", "helper", "volume")


@dataclass(frozen=True)
class _ResourceJournal:
    attempt_token: str
    participant_name: str
    helper_name: str
    volume_name: str
    runtime_command: Tuple[str, ...]
    daemon_id: str
    observed_roles: Tuple[str, ...] = ()


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _resource_journal_path(output_directory: Path) -> Path:
    return output_directory / RESOURCE_JOURNAL_NAME


def _resource_lock_path(output_directory: Path) -> Path:
    return output_directory / RESOURCE_LOCK_NAME


def validate_trusted_directory_ancestors(directory: Path, label: str) -> Path:
    """Resolve a directory and require a non-replaceable trusted ancestry."""

    try:
        canonical = directory.resolve(strict=True)
    except OSError as exc:
        raise InfrastructureUnavailable(
            f"{label}의 실제 경로를 확인할 수 없습니다: {exc}"
        ) from exc

    trusted_owners = {0, os.geteuid()}
    ancestors = (*reversed(canonical.parents), canonical)
    for ancestor in ancestors:
        try:
            metadata = ancestor.lstat()
        except OSError as exc:
            raise InfrastructureUnavailable(
                f"{label}의 상위 경로를 확인할 수 없습니다: "
                f"{ancestor}: {exc}"
            ) from exc
        mode = stat.S_IMODE(metadata.st_mode)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid not in trusted_owners
        ):
            raise InfrastructureUnavailable(
                f"{label}의 상위 경로는 root 또는 현재 운영자 "
                f"소유의 실제 디렉터리여야 합니다: {ancestor}"
            )
        if mode & 0o022 and not mode & stat.S_ISVTX:
            raise InfrastructureUnavailable(
                f"{label}의 group/other 쓰기 가능 상위 경로는 "
                f"sticky bit가 필요합니다: {ancestor}"
            )
    return canonical


def validate_operator_directory(directory: Path, label: str) -> Path:
    """Return one canonical operator-owned 0700 directory."""

    canonical = validate_trusted_directory_ancestors(directory, label)
    try:
        metadata = canonical.lstat()
    except OSError as exc:
        raise InfrastructureUnavailable(
            f"{label}를 확인할 수 없습니다: {exc}"
        ) from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise InfrastructureUnavailable(
            f"{label}는 운영자 소유 권한 0700 디렉터리여야 합니다."
        )
    return canonical


def prepare_operator_directory(
    directory: Path,
    label: str,
    *,
    parents: bool = False,
) -> Path:
    """Create missing private components below one trusted canonical parent."""

    try:
        prospective = directory.resolve(strict=False)
    except OSError as exc:
        raise InfrastructureUnavailable(
            f"{label}의 실제 경로를 확인할 수 없습니다: {exc}"
        ) from exc
    if prospective.exists():
        return validate_operator_directory(prospective, label)

    missing = []
    existing = prospective
    while not existing.exists():
        if existing == existing.parent:
            raise InfrastructureUnavailable(
                f"{label}의 기존 상위 경로를 찾을 수 없습니다."
            )
        missing.append(existing.name)
        existing = existing.parent
    if not parents and len(missing) != 1:
        raise InfrastructureUnavailable(
            f"{label}의 바로 위 디렉터리가 존재해야 합니다."
        )

    current = validate_trusted_directory_ancestors(existing, label)
    for name in reversed(missing):
        candidate = current / name
        try:
            candidate.mkdir(mode=0o700)
        except FileExistsError:
            pass
        except OSError as exc:
            raise InfrastructureUnavailable(
                f"{label}를 준비할 수 없습니다: {exc}"
            ) from exc
        current = validate_operator_directory(candidate, label)
    return current


@contextmanager
def _exclusive_resource_lock(output_directory: Path) -> Iterator[None]:
    path = _resource_lock_path(output_directory)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = -1
    try:
        descriptor = os.open(str(path), flags, 0o600)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise InfrastructureUnavailable(
                "운영자 자원 잠금 파일이 일반 파일이 아닙니다."
            )
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ResourceLockUnavailable(
                "같은 출력 디렉터리에서 다른 평가가 실행 중입니다."
            ) from exc
    except InfrastructureUnavailable:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        raise InfrastructureUnavailable(
            f"운영자 자원 잠금을 준비할 수 없습니다: {exc}"
        ) from exc
    try:
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


@contextmanager
def exclusive_evaluation_lock(output_directory: Path) -> Iterator[Path]:
    """Serialize one complete official evaluation rooted at a directory."""

    output_directory = validate_operator_directory(
        output_directory,
        "운영자 평가 출력 디렉터리",
    )
    with _exclusive_resource_lock(output_directory):
        yield output_directory


def _journal_to_dict(journal: _ResourceJournal) -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "attempt_token": journal.attempt_token,
        "participant_container": journal.participant_name,
        "helper_container": journal.helper_name,
        "output_volume": journal.volume_name,
        "runtime_command": list(journal.runtime_command),
        "daemon_id": journal.daemon_id,
        "observed_roles": list(journal.observed_roles),
    }


def _write_resource_journal(
    output_directory: Path,
    journal: _ResourceJournal,
) -> None:
    path = _resource_journal_path(output_directory)
    temporary = path.with_name(
        f".{path.name}.operator-{secrets.token_hex(8)}"
    )
    payload = (
        json.dumps(
            _journal_to_dict(journal),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    if len(payload) > RESOURCE_JOURNAL_MAX_BYTES:
        raise InfrastructureUnavailable(
            "운영자 자원 정리 저널이 크기 한도를 넘었습니다."
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
        path.chmod(0o600)
        _fsync_directory(output_directory)
    except OSError as exc:
        raise InfrastructureUnavailable(
            f"운영자 자원 정리 저널을 기록할 수 없습니다: {exc}"
        ) from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _record_resource_observed(
    output_directory: Path,
    journal: _ResourceJournal,
    role: str,
) -> _ResourceJournal:
    if role not in _RESOURCE_ROLES:
        raise ValueError(f"알 수 없는 Docker 자원 역할: {role}")
    if role in journal.observed_roles:
        return journal
    observed = set(journal.observed_roles)
    observed.add(role)
    updated = replace(
        journal,
        observed_roles=tuple(
            item for item in _RESOURCE_ROLES if item in observed
        ),
    )
    _write_resource_journal(output_directory, updated)
    return updated


def _load_resource_journal(
    output_directory: Path,
) -> Optional[_ResourceJournal]:
    path = _resource_journal_path(output_directory)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(str(path), flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise InfrastructureUnavailable(
            f"운영자 자원 정리 저널을 열 수 없습니다: {exc}"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size > RESOURCE_JOURNAL_MAX_BYTES
        ):
            raise InfrastructureUnavailable(
                "운영자 자원 정리 저널의 파일 형식이 올바르지 않습니다."
            )
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            payload = stream.read(RESOURCE_JOURNAL_MAX_BYTES + 1)
    finally:
        os.close(descriptor)
    try:
        value = json.loads(payload)
        if not isinstance(value, dict) or set(value) != {
            "schema_version",
            "attempt_token",
            "participant_container",
            "helper_container",
            "output_volume",
            "runtime_command",
            "daemon_id",
            "observed_roles",
        }:
            raise TypeError("저널 필드 오류")
        token = value["attempt_token"]
        participant_name = value["participant_container"]
        helper_name = value["helper_container"]
        volume_name = value["output_volume"]
        runtime_command = value["runtime_command"]
        daemon_id = value["daemon_id"]
        observed = value["observed_roles"]
        if (
            isinstance(value["schema_version"], bool)
            or value["schema_version"] != 1
        ):
            raise ValueError("저널 버전 오류")
        if not isinstance(token, str) or re.fullmatch(
            r"[0-9a-f]{32}", token
        ) is None:
            raise ValueError("실행 식별값 오류")
        resource_name = re.compile(r"[a-z0-9][a-z0-9_.-]{0,127}")
        if (
            not isinstance(participant_name, str)
            or resource_name.fullmatch(participant_name) is None
            or participant_name != f"ossp-router-participant-{token}"
            or not isinstance(helper_name, str)
            or resource_name.fullmatch(helper_name) is None
            or not isinstance(volume_name, str)
            or resource_name.fullmatch(volume_name) is None
        ):
            raise ValueError("자원 이름 오류")
        if (
            not isinstance(runtime_command, list)
            or not runtime_command
            or any(
                not isinstance(item, str)
                or not item
                or len(item.encode("utf-8")) > 4096
                for item in runtime_command
            )
        ):
            raise ValueError("Docker 실행 명령 오류")
        if (
            not isinstance(daemon_id, str)
            or re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}",
                daemon_id,
            )
            is None
        ):
            raise ValueError("Docker Engine ID 오류")
        if (
            not isinstance(observed, list)
            or any(
                not isinstance(role, str) or role not in _RESOURCE_ROLES
                for role in observed
            )
            or len(set(observed)) != len(observed)
        ):
            raise ValueError("관찰한 자원 역할 오류")
    except (
        RecursionError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        raise InfrastructureUnavailable(
            f"운영자 자원 정리 저널을 해석할 수 없습니다: {exc}"
        ) from exc
    return _ResourceJournal(
        attempt_token=token,
        participant_name=participant_name,
        helper_name=helper_name,
        volume_name=volume_name,
        runtime_command=tuple(runtime_command),
        daemon_id=daemon_id,
        observed_roles=tuple(observed),
    )


def _bind_cleanup_attempt_id(
    result: AttemptResult,
    output_directory: Path,
) -> AttemptResult:
    """Bind a pending result to the exact persisted resource-journal attempt."""

    if not result.cleanup_pending or result.cleanup_attempt_id is not None:
        return result
    try:
        journal = _load_resource_journal(output_directory)
    except InfrastructureUnavailable:
        return result
    if journal is None:
        return result
    return replace(result, cleanup_attempt_id=journal.attempt_token)


def _remove_resource_journal(output_directory: Path) -> None:
    try:
        _resource_journal_path(output_directory).unlink()
        _fsync_directory(output_directory)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise InfrastructureUnavailable(
            f"운영자 자원 정리 저널을 제거할 수 없습니다: {exc}"
        ) from exc


def _reconcile_resource_journal(
    runtime_command: Sequence[str],
    output_directory: Path,
    *,
    confirm_daemon_restarted: bool = False,
) -> Optional[str]:
    journal = _load_resource_journal(output_directory)
    if journal is None:
        return None
    if tuple(runtime_command) != journal.runtime_command:
        return (
            "정리 저널을 만든 Docker 실행 명령·context와 현재 명령이 "
            "일치하지 않습니다."
        )
    try:
        current_daemon_id = inspect_runtime_daemon_id(runtime_command)
    except InfrastructureUnavailable as exc:
        return str(exc)
    if current_daemon_id != journal.daemon_id:
        return (
            "정리 저널을 만든 Docker Engine ID와 현재 Engine ID가 "
            "일치하지 않습니다."
        )
    observed = set(journal.observed_roles)
    resources = (
        ("participant", "container", journal.participant_name),
        ("helper", "container", journal.helper_name),
        ("volume", "volume", journal.volume_name),
    )
    unresolved = []
    for role, resource, name in resources:
        label, label_error, absent = _inspect_attempt_label(
            runtime_command,
            resource=resource,
            reference=name,
        )
        if label_error is not None:
            return label_error
        if absent:
            if role in observed or confirm_daemon_restarted:
                observed.add(role)
                continue
            unresolved.append(role)
            continue
        if label != journal.attempt_token:
            return (
                "정리 저널의 자원 이름과 다른 실행 식별 라벨이 충돌합니다."
            )
        if role not in observed:
            observed.add(role)
            journal = replace(
                journal,
                observed_roles=tuple(
                    item for item in _RESOURCE_ROLES if item in observed
                ),
            )
            _write_resource_journal(output_directory, journal)
        command = (
            [*runtime_command, "rm", "--force", name]
            if resource == "container"
            else [*runtime_command, "volume", "rm", "--force", name]
        )
        completed, error = _run_control_command(command)
        if (
            error is not None
            or completed is None
            or (
                completed.returncode != 0
                and not _control_target_absent(completed)
            )
        ):
            return _control_failure_detail(
                "정리 저널의 Docker 자원을 제거할 수 없음",
                completed,
                error,
            )
        after_label, after_error, after_absent = _inspect_attempt_label(
            runtime_command,
            resource=resource,
            reference=name,
        )
        if after_error is not None:
            return after_error
        if not after_absent:
            if after_label != journal.attempt_token:
                return (
                    "제거 확인 중 다른 실행 식별 라벨의 자원을 감지했습니다."
                )
            return "정리 저널의 Docker 자원이 제거된 것으로 확인되지 않습니다."

    if unresolved:
        return (
            "Docker 자원 생성 완료 여부가 불확실해 운영자 복구가 필요합니다 "
            f"(미관찰 역할: {', '.join(unresolved)})."
        )
    journal = replace(
        journal,
        observed_roles=tuple(
            item for item in _RESOURCE_ROLES if item in observed
        ),
    )
    _write_resource_journal(output_directory, journal)
    identity_error = _runtime_daemon_identity_error(
        runtime_command,
        journal.daemon_id,
    )
    if identity_error is not None:
        return identity_error
    _remove_resource_journal(output_directory)
    return None


def reconcile_pending_runtime_resources(
    runtime_command: Sequence[str],
    output_directory: Path,
    *,
    confirm_daemon_restarted: bool = False,
) -> Optional[str]:
    """Reconcile one persisted attempt without touching unlabeled resources."""

    if not runtime_command:
        raise ValueError("컨테이너 실행 명령은 비어 있을 수 없습니다.")
    output_directory = validate_operator_directory(
        output_directory,
        "운영자 출력 디렉터리",
    )
    with _exclusive_resource_lock(output_directory):
        return _reconcile_resource_journal(
            runtime_command,
            output_directory,
            confirm_daemon_restarted=confirm_daemon_restarted,
        )


def require_no_pending_runtime_resources(
    runtime_command: Sequence[str],
    output_directories: Sequence[Path],
) -> None:
    """Fail closed before a sibling official or audit Docker mutation."""

    if not runtime_command:
        raise ValueError("컨테이너 실행 명령은 비어 있을 수 없습니다.")
    for output_directory in output_directories:
        output_directory = validate_operator_directory(
            output_directory,
            "운영자 하위 출력 디렉터리",
        )
        journal = _load_resource_journal(output_directory)
        if journal is None:
            continue
        context_detail = (
            ""
            if tuple(runtime_command) == journal.runtime_command
            else " Docker 실행 명령·context도 현재 실행과 다릅니다."
        )
        raise PendingRuntimeResources(
            "이전 공식 또는 감사 실행의 Docker 자원 정리 저널이 남아 "
            f"새 실행을 시작할 수 없습니다.{context_detail}",
            cleanup_attempt_id=journal.attempt_token,
        )


def _run_isolated_container_once_locked(
    runtime_command: Sequence[str],
    *,
    image: str,
    inputs: InputBatch,
    policy: RoutingPolicy,
    input_path: Path,
    output_directory: Path,
    tier: str,
    platform: Optional[str] = None,
    timeout_seconds: float = PHASE_C_CANDIDATE_LIMITS.wall_time_seconds,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    max_log_bytes: int = DEFAULT_MAX_LOG_BYTES,
    max_log_emitted_bytes: int = DEFAULT_MAX_LOG_EMITTED_BYTES,
    termination_grace_seconds: float = DEFAULT_TERMINATION_GRACE_SECONDS,
) -> AttemptResult:
    """Run one tier with a hard quota-backed, per-attempt output workspace."""

    if not runtime_command:
        raise ValueError("컨테이너 실행 명령은 비어 있을 수 없습니다.")
    _require_immutable_image_reference(image)
    if tier not in TIERS:
        raise ValueError(f"알 수 없는 실행 등급: {tier}")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds는 0보다 커야 합니다.")
    if termination_grace_seconds < 0:
        raise ValueError("termination_grace_seconds는 0 이상이어야 합니다.")
    if max_log_bytes < 0:
        raise ValueError("max_log_bytes는 0 이상이어야 합니다.")
    if max_log_emitted_bytes < max_log_bytes:
        raise ValueError(
            "max_log_emitted_bytes는 max_log_bytes 이상이어야 합니다."
        )
    if not input_path.is_file():
        return AttemptResult(
            AttemptKind.INFRASTRUCTURE_FAILURE,
            f"운영자 입력 파일이 존재하지 않음: {input_path}",
        )
    if not output_directory.is_dir():
        return AttemptResult(
            AttemptKind.INFRASTRUCTURE_FAILURE,
            f"운영자 출력 디렉터리가 존재하지 않음: {output_directory}",
        )
    if max_output_bytes < 1:
        raise ValueError("max_output_bytes는 1 이상이어야 합니다.")
    final_output_path = output_directory / Path(CONTAINER_OUTPUT_PATH).name
    try:
        final_output_path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        return AttemptResult(
            AttemptKind.INFRASTRUCTURE_FAILURE,
            f"운영자가 이전 제출 파일을 격리할 수 없음: {exc}",
        )
    try:
        validate_image_runtime_configuration(
            runtime_command,
            image,
            platform=platform,
        )
    except InfrastructureUnavailable as exc:
        return AttemptResult(AttemptKind.INFRASTRUCTURE_FAILURE, str(exc))
    try:
        daemon_id = inspect_runtime_daemon_id(runtime_command)
    except InfrastructureUnavailable as exc:
        return AttemptResult(AttemptKind.INFRASTRUCTURE_FAILURE, str(exc))

    descriptor, cidfile_name = tempfile.mkstemp(prefix=".ossp-router-cid-")
    os.close(descriptor)
    cidfile = Path(cidfile_name)
    cidfile.unlink()
    volume_name = _random_docker_name("ossp-router-output")
    helper_name = _random_docker_name("ossp-router-helper")
    attempt_token = secrets.token_hex(16)
    participant_name = f"ossp-router-participant-{attempt_token}"
    journal = _ResourceJournal(
        attempt_token=attempt_token,
        participant_name=participant_name,
        helper_name=helper_name,
        volume_name=volume_name,
        runtime_command=tuple(runtime_command),
        daemon_id=daemon_id,
    )
    mutation_started = False

    try:
        with tempfile.TemporaryDirectory(
            prefix=".ossp-router-attempt-",
            dir=output_directory,
        ) as temporary:
            attempt_directory = Path(temporary)
            with _managed_output_cleanup(
                runtime_command,
                helper_name=helper_name,
                volume_name=volume_name,
                attempt_token=attempt_token,
                expected_daemon_id=daemon_id,
            ) as cleanup_state:
                result: AttemptResult
                try:
                    attempt_directory = validate_operator_directory(
                        attempt_directory,
                        "운영자 시도 디렉터리",
                    )
                    attempt_output_path = (
                        attempt_directory / final_output_path.name
                    )
                    staged_helper_path = (
                        attempt_directory / OPERATOR_HELPER_PATH.name
                    )
                    shutil.copyfile(OPERATOR_HELPER_PATH, staged_helper_path)
                    staged_helper_path.chmod(0o444)
                    # The helper UID needs a writable bind target. Copy the
                    # trusted script while the directory is private, then
                    # expose only a sticky, non-listable dropbox.
                    attempt_directory.chmod(0o1733)
                    workspace_error = None
                except InfrastructureUnavailable as exc:
                    workspace_error = str(exc)
                except OSError as exc:
                    workspace_error = (
                        f"운영자 출력 도우미를 준비할 수 없음: {exc}"
                    )
                if workspace_error is None:
                    try:
                        _write_resource_journal(output_directory, journal)
                    except InfrastructureUnavailable as exc:
                        workspace_error = str(exc)
                if workspace_error is None:
                    # Remove by the unique name even if the create command
                    # times out after the daemon created the volume.
                    mutation_started = True
                    cleanup_state.volume_created = True
                    workspace_error = _create_output_volume(
                        runtime_command,
                        volume_name,
                        max_output_bytes,
                        attempt_token,
                    )
                    if workspace_error is None:
                        try:
                            journal = _record_resource_observed(
                                output_directory,
                                journal,
                                "volume",
                            )
                        except InfrastructureUnavailable as exc:
                            workspace_error = str(exc)
                if workspace_error is not None:
                    result = AttemptResult(
                        AttemptKind.INFRASTRUCTURE_FAILURE,
                        workspace_error,
                        cleanup_pending=mutation_started,
                    )
                else:
                    # Attempt cleanup by the unique name even if the start
                    # command times out after the daemon created the helper.
                    cleanup_state.helper_start_attempted = True
                    workspace_error = _start_output_helper(
                        runtime_command,
                        helper_name=helper_name,
                        volume_name=volume_name,
                        result_directory=attempt_directory,
                        attempt_token=attempt_token,
                    )
                    if workspace_error is not None:
                        result = AttemptResult(
                            AttemptKind.INFRASTRUCTURE_FAILURE,
                            workspace_error,
                            cleanup_pending=True,
                        )
                    else:
                        try:
                            journal = _record_resource_observed(
                                output_directory,
                                journal,
                                "helper",
                            )
                        except InfrastructureUnavailable as exc:
                            result = AttemptResult(
                                AttemptKind.INFRASTRUCTURE_FAILURE,
                                str(exc),
                                cleanup_pending=True,
                            )
                        else:
                            try:
                                validate_host_core_dump_policy(
                                    runtime_command,
                                    helper_name,
                                )
                            except InfrastructureUnavailable as exc:
                                result = AttemptResult(
                                    AttemptKind.INFRASTRUCTURE_FAILURE,
                                    str(exc),
                                )
                            else:
                                arguments = build_isolated_container_command(
                                    runtime_command,
                                    image=image,
                                    input_path=input_path,
                                    output_volume=volume_name,
                                    container_name=participant_name,
                                    attempt_token=attempt_token,
                                    tier=tier,
                                    platform=platform,
                                    cidfile=cidfile,
                                    termination_grace_seconds=(
                                        termination_grace_seconds
                                    ),
                                )

                                def observe_participant() -> None:
                                    nonlocal journal
                                    journal = _record_resource_observed(
                                        output_directory,
                                        journal,
                                        "participant",
                                    )

                                try:
                                    result = _run_prepared_router_once(
                                        arguments,
                                        inputs=inputs,
                                        policy=policy,
                                        output_path=attempt_output_path,
                                        tier=tier,
                                        timeout_seconds=timeout_seconds,
                                        max_output_bytes=max_output_bytes,
                                        max_log_bytes=max_log_bytes,
                                        max_log_emitted_bytes=(
                                            max_log_emitted_bytes
                                        ),
                                        termination_grace_seconds=(
                                            termination_grace_seconds
                                        ),
                                        monitor_output_path=False,
                                        post_exit=lambda: (
                                            _extract_output_from_helper(
                                                runtime_command,
                                                helper_name=helper_name,
                                                output_path=attempt_output_path,
                                                maximum=max_output_bytes,
                                            )
                                        ),
                                    )
                                except Exception:
                                    if (
                                        _runtime_daemon_identity_error(
                                            runtime_command,
                                            daemon_id,
                                        )
                                        is None
                                    ):
                                        _best_effort_remove_participant_container(
                                            runtime_command,
                                            container_name=participant_name,
                                            attempt_token=attempt_token,
                                            termination_grace_seconds=(
                                                termination_grace_seconds
                                            ),
                                        )
                                    raise
                                identity_error = (
                                    _runtime_daemon_identity_error(
                                        runtime_command,
                                        daemon_id,
                                    )
                                )
                                if identity_error is not None:
                                    result = replace(
                                        _infrastructure_result(
                                            result,
                                            identity_error,
                                        ),
                                        cleanup_pending=True,
                                    )
                                else:
                                    result = (
                                        _reconcile_and_remove_participant_container(
                                            runtime_command,
                                            container_name=participant_name,
                                            attempt_token=attempt_token,
                                            result=result,
                                            termination_grace_seconds=(
                                                termination_grace_seconds
                                            ),
                                            observe_owned=observe_participant,
                                        )
                                    )

            cleanup_errors = cleanup_state.errors
            if cleanup_errors:
                return replace(
                    _infrastructure_result(
                        result,
                        "출력 작업 공간 정리 실패: "
                        + "; ".join(cleanup_errors),
                    ),
                    cleanup_pending=True,
                )
            if result.cleanup_pending:
                return result
            try:
                if _resource_journal_path(output_directory).exists():
                    journal = replace(
                        journal,
                        observed_roles=_RESOURCE_ROLES,
                    )
                    _write_resource_journal(output_directory, journal)
                    identity_error = _runtime_daemon_identity_error(
                        runtime_command,
                        daemon_id,
                    )
                    if identity_error is not None:
                        raise InfrastructureUnavailable(identity_error)
                    _remove_resource_journal(output_directory)
            except InfrastructureUnavailable as exc:
                return AttemptResult(
                    AttemptKind.INFRASTRUCTURE_FAILURE,
                    str(exc),
                    returncode=result.returncode,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    timed_out=result.timed_out,
                    operator_terminated=result.operator_terminated,
                    cleanup_pending=True,
                    output_bytes=result.output_bytes,
                    output_sha256=result.output_sha256,
                )
            if result.kind is not AttemptKind.VALID:
                return result

            publish_path = output_directory / (
                f".{final_output_path.name}.operator-{secrets.token_hex(8)}"
            )
            try:
                shutil.copyfile(attempt_output_path, publish_path)
                publish_path.chmod(0o644)
                os.replace(publish_path, final_output_path)
            except OSError as exc:
                return AttemptResult(
                    AttemptKind.INFRASTRUCTURE_FAILURE,
                    f"운영자가 검증된 제출 파일을 게시할 수 없음: {exc}",
                    returncode=result.returncode,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    output_bytes=result.output_bytes,
                    output_sha256=result.output_sha256,
                )
            finally:
                try:
                    publish_path.unlink()
                except FileNotFoundError:
                    pass
            return result
    finally:
        try:
            cidfile.unlink()
        except FileNotFoundError:
            pass


def run_isolated_container_once(
    runtime_command: Sequence[str],
    *,
    image: str,
    inputs: InputBatch,
    policy: RoutingPolicy,
    input_path: Path,
    output_directory: Path,
    tier: str,
    platform: Optional[str] = None,
    timeout_seconds: float = PHASE_C_CANDIDATE_LIMITS.wall_time_seconds,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    max_log_bytes: int = DEFAULT_MAX_LOG_BYTES,
    max_log_emitted_bytes: int = DEFAULT_MAX_LOG_EMITTED_BYTES,
    termination_grace_seconds: float = DEFAULT_TERMINATION_GRACE_SECONDS,
) -> AttemptResult:
    """Serialize one output directory and fail closed on pending cleanup."""

    if not runtime_command:
        raise ValueError("컨테이너 실행 명령은 비어 있을 수 없습니다.")
    try:
        output_directory = validate_operator_directory(
            output_directory,
            "운영자 출력 디렉터리",
        )
    except InfrastructureUnavailable as exc:
        return AttemptResult(
            AttemptKind.INFRASTRUCTURE_FAILURE,
            str(exc),
        )
    try:
        with _exclusive_resource_lock(output_directory):
            recovery_error = _reconcile_resource_journal(
                runtime_command,
                output_directory,
            )
            if recovery_error is not None:
                return _bind_cleanup_attempt_id(
                    AttemptResult(
                        AttemptKind.INFRASTRUCTURE_FAILURE,
                        recovery_error,
                        cleanup_pending=True,
                    ),
                    output_directory,
                )
            return _bind_cleanup_attempt_id(
                _run_isolated_container_once_locked(
                    runtime_command,
                    image=image,
                    inputs=inputs,
                    policy=policy,
                    input_path=input_path,
                    output_directory=output_directory,
                    tier=tier,
                    platform=platform,
                    timeout_seconds=timeout_seconds,
                    max_output_bytes=max_output_bytes,
                    max_log_bytes=max_log_bytes,
                    max_log_emitted_bytes=max_log_emitted_bytes,
                    termination_grace_seconds=termination_grace_seconds,
                ),
                output_directory,
            )
    except ResourceLockUnavailable as exc:
        return AttemptResult(
            AttemptKind.INFRASTRUCTURE_FAILURE,
            str(exc),
        )
    except InfrastructureUnavailable as exc:
        return _bind_cleanup_attempt_id(
            AttemptResult(
                AttemptKind.INFRASTRUCTURE_FAILURE,
                str(exc),
                cleanup_pending=_resource_journal_path(output_directory).exists(),
            ),
            output_directory,
        )


def run_with_retry(
    attempt: Callable[[int], AttemptResult],
    *,
    max_participant_attempts: int = DEFAULT_MAX_PARTICIPANT_ATTEMPTS,
    max_infrastructure_failures: int = DEFAULT_MAX_INFRASTRUCTURE_FAILURES,
) -> RetryOutcome:
    """Use the first valid result; count only participant execution failures."""

    if max_participant_attempts < 1:
        raise ValueError("max_participant_attempts는 1 이상이어야 합니다.")
    if max_infrastructure_failures < 1:
        raise ValueError("max_infrastructure_failures는 1 이상이어야 합니다.")
    official_attempts = 0
    infrastructure_failures = 0
    history = []
    while official_attempts < max_participant_attempts:
        result = attempt(official_attempts + 1)
        history.append(result)
        if result.kind is AttemptKind.VALID:
            if result.submission is None:
                raise ValueError("VALID 결과에는 submission이 필요합니다.")
            return RetryOutcome(
                selected_submission=result.submission,
                official_attempts=official_attempts + 1,
                infrastructure_failures=infrastructure_failures,
                history=tuple(history),
                tier_score_zero=False,
                disqualified=False,
            )
        if result.kind is AttemptKind.FAIRNESS_VIOLATION:
            return RetryOutcome(
                selected_submission=None,
                official_attempts=official_attempts,
                infrastructure_failures=infrastructure_failures,
                history=tuple(history),
                tier_score_zero=False,
                disqualified=True,
            )
        if result.kind is AttemptKind.INFRASTRUCTURE_FAILURE:
            infrastructure_failures += 1
            if (
                result.cleanup_pending
                or infrastructure_failures >= max_infrastructure_failures
            ):
                return RetryOutcome(
                    selected_submission=None,
                    official_attempts=official_attempts,
                    infrastructure_failures=infrastructure_failures,
                    history=tuple(history),
                    tier_score_zero=False,
                    disqualified=False,
                    infrastructure_unavailable=True,
                )
            continue
        if result.kind is not AttemptKind.EXECUTION_FAILURE:
            raise ValueError(f"알 수 없는 실행 결과: {result.kind}")
        official_attempts += 1
    return RetryOutcome(
        selected_submission=None,
        official_attempts=official_attempts,
        infrastructure_failures=infrastructure_failures,
        history=tuple(history),
        tier_score_zero=True,
        disqualified=False,
    )


def cleanup_main(argv: Optional[Sequence[str]] = None) -> int:
    """Reconcile a persisted Docker mutation intent after operator review."""

    parser = argparse.ArgumentParser(
        prog="router-cleanup-resources",
        description=(
            "불확실한 Docker 생성 요청의 영속 정리 저널을 복구합니다."
        ),
    )
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--runtime", default="docker")
    parser.add_argument("--docker-context")
    parser.add_argument(
        "--confirm-daemon-restarted",
        action="store_true",
        help=(
            "Docker 데몬 재시작 뒤 계속 부재한 저널 자원을 해소합니다."
        ),
    )
    args = parser.parse_args(argv)
    runtime_command = [args.runtime]
    if args.docker_context:
        runtime_command.extend(["--context", args.docker_context])
    try:
        error = reconcile_pending_runtime_resources(
            runtime_command,
            args.output_directory,
            confirm_daemon_restarted=args.confirm_daemon_restarted,
        )
    except (InfrastructureUnavailable, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 4
    if error is not None:
        print(f"ERROR: {error}", file=sys.stderr)
        return 4
    print("OK: 보류 중인 Docker 자원 정리 저널이 없습니다.")
    return 0
