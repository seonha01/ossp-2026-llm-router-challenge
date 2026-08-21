# SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
# SPDX-License-Identifier: Apache-2.0

"""Operator-only latency tiebreak measurement for exactly tied submissions."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .orchestrator import (
    ResolvedImage,
    _prepare_operator_directory,
    _prepare_output_child_directory,
    _private_input_snapshot,
    _validate_private_work_directory,
    _validate_record_path,
    _write_record_atomic,
    resolve_submitted_image,
)
from .protocol import (
    TIERS,
    InputBatch,
    ProtocolError,
    RoutingPolicy,
    load_bundled_policy,
    load_input,
    load_policy,
)
from .runtime import (
    DEFAULT_MAX_INFRASTRUCTURE_FAILURES,
    OFFICIAL_CONTAINER_PLATFORM,
    PHASE_C_CANDIDATE_LIMITS,
    AttemptKind,
    AttemptResult,
    ImagePreflightRejected,
    ImageSizeEvidence,
    InfrastructureUnavailable,
    RuntimeResourceLimits,
    exclusive_evaluation_lock,
    is_immutable_image_reference,
    load_image_size_evidence,
    require_no_pending_runtime_resources,
    run_isolated_container_once,
)


WARMUP_RUNS_PER_TIER = 1
MEASURED_RUNS_PER_TIER = 5
_COMMIT_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SUBMISSION_ID = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,127})$")
_EXIT_CONFIGURATION_ERROR = 2
_EXIT_INFRASTRUCTURE_UNAVAILABLE = 4
_EXIT_SUBMISSION_PREFLIGHT_REJECTED = 6
_CANDIDATE_FILE_MAX_BYTES = 1024 * 1024


@dataclass(frozen=True)
class LatencyCandidate:
    """One already score-tied and non-disqualified technical submission."""

    submission_id: str
    commit_sha: str
    submitted_image_digest: str
    image_size_evidence: ImageSizeEvidence


@dataclass(frozen=True)
class _PreparedCandidate:
    candidate: LatencyCandidate
    resolved_image: ResolvedImage
    output_directory: Path


ImageResolver = Callable[
    [Sequence[str], str, Optional[ImageSizeEvidence]],
    ResolvedImage,
]
ContainerRunner = Callable[..., AttemptResult]


def _limits_to_dict(limits: RuntimeResourceLimits) -> Dict[str, int]:
    return {
        "cpu_cores": limits.cpu_cores,
        "wall_time_seconds": limits.wall_time_seconds,
        "memory_bytes": limits.memory_bytes,
        "memory_swap_total_bytes": limits.memory_swap_total_bytes,
        "processes_and_threads_total": limits.processes_and_threads_total,
        "temporary_storage_bytes": limits.temporary_storage_bytes,
        "output_bytes": limits.output_bytes,
        "output_inodes": limits.output_inodes,
        "retained_log_bytes_per_stream": limits.retained_log_bytes_per_stream,
        "emitted_log_bytes_per_stream": limits.emitted_log_bytes_per_stream,
        "oci_compressed_image_bytes": limits.oci_compressed_image_bytes,
        "rootfs_apparent_bytes": limits.rootfs_apparent_bytes,
    }


def _timer_resolution_ns() -> int:
    resolution = time.get_clock_info("perf_counter").resolution
    return max(1, int(math.ceil(resolution * 1_000_000_000)))


def _comparison_time_ns(elapsed_ns: int, resolution_ns: int) -> int:
    """Round one ranking total to the nearest advertised timer tick."""

    if elapsed_ns < 0:
        raise ValueError("elapsed_ns는 0 이상이어야 합니다.")
    if resolution_ns < 1:
        raise ValueError("timer_resolution_ns는 1 이상이어야 합니다.")
    return ((elapsed_ns + resolution_ns // 2) // resolution_ns) * resolution_ns


def _median_of_five(values: Sequence[int]) -> int:
    if len(values) != MEASURED_RUNS_PER_TIER:
        raise ValueError("등급별 공식 측정값은 정확히 5개여야 합니다.")
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0
           for value in values):
        raise ValueError("레이턴시 측정값은 0 이상의 정수 나노초여야 합니다.")
    return sorted(values)[MEASURED_RUNS_PER_TIER // 2]


def _rank_candidates(
    totals: Mapping[str, int],
    *,
    timer_resolution_ns: int,
) -> List[Dict[str, Any]]:
    rows = [
        {
            "submission_id": submission_id,
            "total_of_tier_medians_ns": total,
            "comparison_time_ns": _comparison_time_ns(
                total,
                timer_resolution_ns,
            ),
        }
        for submission_id, total in totals.items()
    ]
    rows.sort(
        key=lambda row: (
            row["comparison_time_ns"],
            row["submission_id"],
        )
    )
    previous = None
    current_rank = 0
    for position, row in enumerate(rows, start=1):
        if row["comparison_time_ns"] != previous:
            current_rank = position
            previous = row["comparison_time_ns"]
        row["rank"] = current_rank
    return rows


def _validate_candidate_set(candidates: Sequence[LatencyCandidate]) -> None:
    if len(candidates) < 2:
        raise ValueError("동점 측정에는 두 개 이상의 제출이 필요합니다.")
    seen = set()
    for candidate in candidates:
        if _SUBMISSION_ID.fullmatch(candidate.submission_id) is None:
            raise ValueError(
                "submission_id는 1~128자의 영문자, 숫자, '.', '_', '-'만 "
                "사용해야 합니다."
            )
        if candidate.submission_id in seen:
            raise ValueError(f"중복 submission_id: {candidate.submission_id}")
        seen.add(candidate.submission_id)
        if _COMMIT_SHA.fullmatch(candidate.commit_sha) is None:
            raise ValueError(
                "커밋 SHA는 40자 또는 64자 소문자 16진수여야 합니다."
            )


def _validate_resolved_image(
    resolved: ResolvedImage,
    limits: RuntimeResourceLimits,
) -> str:
    execution_image = resolved.execution_image_reference or resolved.local_image_id
    if (
        not is_immutable_image_reference(resolved.local_image_id)
        or not is_immutable_image_reference(resolved.selected_manifest_digest)
        or re.fullmatch(r"[0-9a-f]{64}", resolved.size_evidence_sha256) is None
        or not is_immutable_image_reference(execution_image)
    ):
        raise InfrastructureUnavailable(
            "운영자가 확인한 이미지 다이제스트 또는 크기 증거 해시가 "
            "올바르지 않습니다."
        )
    if f"{resolved.operating_system}/{resolved.architecture}" != (
        OFFICIAL_CONTAINER_PLATFORM
    ):
        raise InfrastructureUnavailable(
            "동점 측정 이미지가 공식 컨테이너 플랫폼과 일치하지 않습니다."
        )
    for name, value, maximum in (
        (
            "OCI 압축 계층 합계",
            resolved.oci_compressed_layer_bytes,
            limits.oci_compressed_image_bytes,
        ),
        (
            "rootfs 겉보기 크기",
            resolved.rootfs_apparent_bytes,
            limits.rootfs_apparent_bytes,
        ),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise InfrastructureUnavailable(
                f"운영자가 확인한 {name}가 올바르지 않습니다."
            )
        if value > maximum:
            raise ImagePreflightRejected(f"{name}가 확정 한도를 초과합니다.")
    return execution_image


def _run_same_condition(
    *,
    prepared: _PreparedCandidate,
    runtime_command: Sequence[str],
    inputs: InputBatch,
    policy: RoutingPolicy,
    input_path: Path,
    tier: str,
    limits: RuntimeResourceLimits,
    container_runner: ContainerRunner,
    measured: bool,
    max_infrastructure_failures: int,
) -> Tuple[Optional[Dict[str, Any]], int]:
    """Discard operator failures and retry the same candidate/tier condition."""

    infrastructure_failures = 0
    while True:
        result = container_runner(
            runtime_command,
            image=(
                prepared.resolved_image.execution_image_reference
                or prepared.resolved_image.local_image_id
            ),
            platform=OFFICIAL_CONTAINER_PLATFORM,
            inputs=inputs,
            policy=policy,
            input_path=input_path,
            output_directory=prepared.output_directory,
            tier=tier,
            timeout_seconds=limits.wall_time_seconds,
            max_output_bytes=limits.output_bytes,
            max_log_bytes=limits.retained_log_bytes_per_stream,
            max_log_emitted_bytes=limits.emitted_log_bytes_per_stream,
        )
        if result.kind is AttemptKind.INFRASTRUCTURE_FAILURE:
            infrastructure_failures += 1
            if (
                result.cleanup_pending
                or infrastructure_failures >= max_infrastructure_failures
            ):
                raise InfrastructureUnavailable(
                    "같은 동점 측정 조건에서 운영자 장애가 반복되었습니다."
                )
            continue
        if result.kind is AttemptKind.FAIRNESS_VIOLATION:
            raise ValueError(
                f"금지 전략 검토가 필요한 동점 제출: "
                f"{prepared.candidate.submission_id}"
            )
        if not measured:
            return None, infrastructure_failures
        if result.kind is AttemptKind.EXECUTION_FAILURE:
            return (
                {
                    "result": "participant_failure_limit_assigned",
                    "elapsed_ns": limits.wall_time_seconds * 1_000_000_000,
                },
                infrastructure_failures,
            )
        if result.kind is not AttemptKind.VALID or result.submission is None:
            raise InfrastructureUnavailable(
                "실행 결과 분류가 동점 측정 계약과 일치하지 않습니다."
            )
        elapsed_ns = result.measurement_elapsed_ns
        if (
            isinstance(elapsed_ns, bool)
            or not isinstance(elapsed_ns, int)
            or elapsed_ns < 0
        ):
            infrastructure_failures += 1
            if infrastructure_failures >= max_infrastructure_failures:
                raise InfrastructureUnavailable(
                    "유효 실행의 레이턴시를 단조 시계로 확인할 수 없습니다."
                )
            continue
        return (
            {"result": "valid", "elapsed_ns": elapsed_ns},
            infrastructure_failures,
        )


def measure_tiebreak_latency(
    *,
    runtime_command: Sequence[str],
    candidates: Sequence[LatencyCandidate],
    inputs: InputBatch,
    policy: RoutingPolicy,
    input_path: Path,
    output_directory: Path,
    private_work_directory: Path,
    record_path: Path,
    limits: RuntimeResourceLimits = PHASE_C_CANDIDATE_LIMITS,
    timer_resolution_ns: Optional[int] = None,
    image_resolver: ImageResolver = resolve_submitted_image,
    container_runner: ContainerRunner = run_isolated_container_once,
    max_infrastructure_failures: int = DEFAULT_MAX_INFRASTRUCTURE_FAILURES,
) -> Mapping[str, Any]:
    """Measure and rank already score-tied submissions under the official path."""

    if not runtime_command:
        raise ValueError("컨테이너 실행 명령은 비어 있을 수 없습니다.")
    _validate_candidate_set(candidates)
    if max_infrastructure_failures < 1:
        raise ValueError("max_infrastructure_failures는 1 이상이어야 합니다.")
    resolution_ns = (
        _timer_resolution_ns()
        if timer_resolution_ns is None
        else timer_resolution_ns
    )
    if isinstance(resolution_ns, bool) or not isinstance(resolution_ns, int):
        raise ValueError("timer_resolution_ns는 정수여야 합니다.")
    if resolution_ns < 1:
        raise ValueError("timer_resolution_ns는 1 이상이어야 합니다.")

    output_directory = _prepare_operator_directory(
        output_directory,
        "동점 측정 출력 디렉터리",
        parents=True,
    )
    with exclusive_evaluation_lock(output_directory) as locked_output:
        record_path = _validate_record_path(record_path, locked_output)
        private_work_directory = _validate_private_work_directory(
            private_work_directory,
            locked_output,
        )
        prepared_candidates = []
        for index, candidate in enumerate(candidates):
            candidate_output = _prepare_output_child_directory(
                locked_output,
                f"candidate-{index + 1:03d}",
                "동점 제출 실행 출력 디렉터리",
            )
            resolved = image_resolver(
                runtime_command,
                candidate.submitted_image_digest,
                candidate.image_size_evidence,
            )
            _validate_resolved_image(resolved, limits)
            prepared_candidates.append(
                _PreparedCandidate(candidate, resolved, candidate_output)
            )
        require_no_pending_runtime_resources(
            runtime_command,
            tuple(item.output_directory for item in prepared_candidates),
        )

        candidate_records: Dict[str, Dict[str, Any]] = {}
        for prepared in prepared_candidates:
            candidate_records[prepared.candidate.submission_id] = {
                "submission_id": prepared.candidate.submission_id,
                "commit_sha": prepared.candidate.commit_sha,
                "image": {
                    "submitted_digest": (
                        prepared.resolved_image.submitted_digest
                    ),
                    "selected_manifest_digest": (
                        prepared.resolved_image.selected_manifest_digest
                    ),
                    "size_evidence_sha256": (
                        prepared.resolved_image.size_evidence_sha256
                    ),
                },
                "tiers": {},
            }

        with _private_input_snapshot(
            input_path,
            inputs,
            private_work_directory,
        ) as snapshot:
            for tier_index, tier in enumerate(TIERS):
                warmup_order = (
                    prepared_candidates[tier_index % len(prepared_candidates):]
                    + prepared_candidates[:tier_index % len(prepared_candidates)]
                )
                for prepared in warmup_order:
                    _, discarded = _run_same_condition(
                        prepared=prepared,
                        runtime_command=runtime_command,
                        inputs=inputs,
                        policy=policy,
                        input_path=snapshot.path,
                        tier=tier,
                        limits=limits,
                        container_runner=container_runner,
                        measured=False,
                        max_infrastructure_failures=max_infrastructure_failures,
                    )
                    candidate_records[prepared.candidate.submission_id]["tiers"][
                        tier
                    ] = {
                        "warmup_completed": True,
                        "measurements": [],
                        "discarded_infrastructure_attempts": discarded,
                    }

                for repetition in range(1, MEASURED_RUNS_PER_TIER + 1):
                    offset = (repetition - 1) % len(prepared_candidates)
                    alternating_order = (
                        prepared_candidates[offset:]
                        + prepared_candidates[:offset]
                    )
                    for order_position, prepared in enumerate(
                        alternating_order,
                        start=1,
                    ):
                        measurement, discarded = _run_same_condition(
                            prepared=prepared,
                            runtime_command=runtime_command,
                            inputs=inputs,
                            policy=policy,
                            input_path=snapshot.path,
                            tier=tier,
                            limits=limits,
                            container_runner=container_runner,
                            measured=True,
                            max_infrastructure_failures=(
                                max_infrastructure_failures
                            ),
                        )
                        assert measurement is not None
                        measurement.update(
                            {
                                "repetition": repetition,
                                "order_position": order_position,
                            }
                        )
                        tier_record = candidate_records[
                            prepared.candidate.submission_id
                        ]["tiers"][tier]
                        tier_record["measurements"].append(measurement)
                        tier_record["discarded_infrastructure_attempts"] += (
                            discarded
                        )

        totals: Dict[str, int] = {}
        for submission_id, candidate_record in candidate_records.items():
            medians = []
            for tier in TIERS:
                tier_record = candidate_record["tiers"][tier]
                median_ns = _median_of_five(
                    [
                        measurement["elapsed_ns"]
                        for measurement in tier_record["measurements"]
                    ]
                )
                tier_record["median_ns"] = median_ns
                medians.append(median_ns)
            totals[submission_id] = sum(medians)

        ranking = _rank_candidates(
            totals,
            timer_resolution_ns=resolution_ns,
        )
        ranking_by_id = {row["submission_id"]: row for row in ranking}
        for submission_id, candidate_record in candidate_records.items():
            basis = ranking_by_id[submission_id]
            candidate_record["total_of_tier_medians_ns"] = basis[
                "total_of_tier_medians_ns"
            ]
            candidate_record["comparison_time_ns"] = basis[
                "comparison_time_ns"
            ]
            candidate_record["rank"] = basis["rank"]

        report: Mapping[str, Any] = {
            "schema_version": 1,
            "report_type": "official-tiebreak-latency-record",
            "status": "complete",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "platform": OFFICIAL_CONTAINER_PLATFORM,
            "policy_id": policy.policy_id,
            "measurement_protocol": {
                "timer": "perf_counter_monotonic",
                "timer_resolution_ns": resolution_ns,
                "measurement_interval": (
                    "docker-run-start-through-v1-validation"
                ),
                "warmup_runs_per_tier": WARMUP_RUNS_PER_TIER,
                "measured_runs_per_tier": MEASURED_RUNS_PER_TIER,
                "submission_order": "cyclic-rotation-by-repetition",
                "tier_aggregate": "median-of-five",
                "final_aggregate": "sum-of-tier-medians",
                "timer_tie_comparison": "nearest-timer-resolution",
                "participant_failure_elapsed_ns": (
                    limits.wall_time_seconds * 1_000_000_000
                ),
                "infrastructure_failures": "discard-and-repeat-same-condition",
            },
            "resource_limits": _limits_to_dict(limits),
            "candidates": list(candidate_records.values()),
            "ranking": ranking,
        }
        _write_record_atomic(record_path, report)
        return report


def _reject_duplicate_keys(pairs: Sequence[Tuple[str, Any]]) -> Dict[str, Any]:
    value: Dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"중복 JSON 키: {key}")
        value[key] = item
    return value


def load_latency_candidates(path: Path) -> Tuple[LatencyCandidate, ...]:
    """Load the private operator candidate list and its evidence references."""

    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"동점 제출 목록을 읽을 수 없습니다: {exc}") from exc
    if len(payload) > _CANDIDATE_FILE_MAX_BYTES:
        raise ValueError("동점 제출 목록이 허용 크기를 넘었습니다.")
    try:
        value = json.loads(payload, object_pairs_hook=_reject_duplicate_keys)
    except (RecursionError, UnicodeError, ValueError) as exc:
        raise ValueError(f"동점 제출 목록 JSON이 올바르지 않습니다: {exc}") from exc
    if not isinstance(value, dict) or set(value) != {"schema_version", "candidates"}:
        raise ValueError(
            "동점 제출 목록은 schema_version과 candidates만 포함해야 합니다."
        )
    if value["schema_version"] != 1 or isinstance(value["schema_version"], bool):
        raise ValueError("동점 제출 목록 schema_version은 정수 1이어야 합니다.")
    raw_candidates = value["candidates"]
    if not isinstance(raw_candidates, list):
        raise ValueError("candidates는 배열이어야 합니다.")
    candidates = []
    for raw in raw_candidates:
        if not isinstance(raw, dict) or set(raw) != {
            "submission_id",
            "commit_sha",
            "image",
            "image_size_evidence",
        }:
            raise ValueError(
                "각 candidate에는 submission_id, commit_sha, image, "
                "image_size_evidence가 필요합니다."
            )
        if not all(isinstance(raw[key], str) for key in raw):
            raise ValueError("candidate의 모든 값은 문자열이어야 합니다.")
        evidence_path = Path(raw["image_size_evidence"])
        if not evidence_path.is_absolute():
            evidence_path = path.parent / evidence_path
        candidates.append(
            LatencyCandidate(
                submission_id=raw["submission_id"],
                commit_sha=raw["commit_sha"],
                submitted_image_digest=raw["image"],
                image_size_evidence=load_image_size_evidence(evidence_path),
            )
        )
    _validate_candidate_set(candidates)
    return tuple(candidates)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="router-measure-tiebreak",
        description="정확히 동점인 제출의 공식 라우터 레이턴시를 측정합니다.",
    )
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--private-work-directory", type=Path, required=True)
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
        candidates = load_latency_candidates(args.candidates)
        inputs = load_input(args.input)
        policy = load_policy(args.policy) if args.policy else load_bundled_policy()
        measure_tiebreak_latency(
            runtime_command=runtime,
            candidates=candidates,
            inputs=inputs,
            policy=policy,
            input_path=args.input,
            output_directory=args.output_directory,
            private_work_directory=args.private_work_directory,
            record_path=args.record,
        )
    except ImagePreflightRejected as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return _EXIT_SUBMISSION_PREFLIGHT_REJECTED
    except InfrastructureUnavailable as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return _EXIT_INFRASTRUCTURE_UNAVAILABLE
    except (OSError, ProtocolError, ValueError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return _EXIT_CONFIGURATION_ERROR
    print(f"OK: 동점 레이턴시 기록을 {args.record}에 저장했습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
