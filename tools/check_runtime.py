# SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
# SPDX-License-Identifier: Apache-2.0

"""Run a participant image on materialized public Train/Dev inputs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Mapping, Optional, Sequence

from ossp_router.orchestrator import input_batch_to_dict
from ossp_router.protocol import TIERS, load_bundled_policy, load_input, write_json
from ossp_router.public_runtime import (
    PUBLIC_RUNTIME_PROFILE_ID,
    load_public_runtime_workload,
)
from ossp_router.runtime import (
    CONTAINER_INPUT_PATH,
    CONTAINER_OUTPUT_DIRECTORY,
    CONTAINER_OUTPUT_PATH,
    CONTAINER_TEMPORARY_DIRECTORY,
    CONTAINER_USER,
    DEFAULT_TERMINATION_GRACE_SECONDS,
    OFFICIAL_CONTAINER_PLATFORM,
    PHASE_C_CANDIDATE_LIMITS,
    AttemptKind,
    AttemptResult,
    InfrastructureUnavailable,
    _run_prepared_router_once,
    inspect_image_runtime_metadata,
    is_immutable_image_reference,
    validate_image_configuration,
)
ROOT = pathlib.Path(__file__).resolve().parents[1]
REPORT_SCHEMA_VERSION = 1
DEFAULT_TRAIN_INPUT = ROOT / "data/materialized/train/inputs.json"
DEFAULT_DEV_INPUT = ROOT / "data/materialized/dev/inputs.json"
DEFAULT_PUBLIC_DATA_REGISTRY = ROOT / "data/public-data.v1.json"


def _docker_size(value: int) -> str:
    gibibyte = 1024 * 1024 * 1024
    mebibyte = 1024 * 1024
    if value % gibibyte == 0:
        return f"{value // gibibyte}g"
    if value % mebibyte == 0:
        return f"{value // mebibyte}m"
    return str(value)


def _resolve_docker(command: str) -> str:
    resolved = shutil.which(command)
    if resolved is None:
        raise RuntimeError(f"Docker 호환 CLI를 찾을 수 없습니다: {command}")
    return resolved


def _runtime_platform(docker: str) -> Optional[str]:
    try:
        completed = subprocess.run(
            [
                docker,
                "info",
                "--format",
                "{{json .OSType}} {{json .Architecture}}",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    try:
        operating_system, architecture = (
            json.loads(item) for item in completed.stdout.split()
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(operating_system, str) or not isinstance(
        architecture, str
    ):
        return None
    normalized_architecture = (
        "arm64" if architecture == "aarch64" else architecture
    )
    return f"{operating_system}/{normalized_architecture}"


def _resolve_image(docker: str, image: str) -> str:
    try:
        metadata = inspect_image_runtime_metadata(
            [docker],
            image,
            platform=OFFICIAL_CONTAINER_PLATFORM,
        )
        validate_image_configuration(metadata)
    except (InfrastructureUnavailable, ValueError) as exc:
        raise RuntimeError(str(exc)) from exc
    image_id = metadata.get("Id")
    operating_system = metadata.get("Os")
    architecture = metadata.get("Architecture")
    normalized_architecture = (
        "arm64" if architecture == "aarch64" else architecture
    )
    if operating_system != "linux" or normalized_architecture != "arm64":
        raise RuntimeError("검사할 이미지가 linux/arm64가 아닙니다.")
    if not isinstance(image_id, str) or not is_immutable_image_reference(
        image_id
    ):
        raise RuntimeError("로컬 이미지의 변경 불가능한 ID를 확인할 수 없습니다.")
    return image_id


def _container_arguments(
    *,
    docker: str,
    image_id: str,
    input_path: pathlib.Path,
    output_directory: pathlib.Path,
    tier: str,
    cidfile: pathlib.Path,
    container_name: str,
) -> List[str]:
    limits = PHASE_C_CANDIDATE_LIMITS
    return [
        docker,
        "run",
        "--rm",
        "--pull",
        "never",
        "--cidfile",
        str(cidfile),
        "--log-driver",
        "none",
        "--stop-timeout",
        str(int(DEFAULT_TERMINATION_GRACE_SECONDS)),
        "--stop-signal",
        "SIGTERM",
        "--no-healthcheck",
        "--name",
        container_name,
        "--platform",
        OFFICIAL_CONTAINER_PLATFORM,
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
            f"type=bind,src={output_directory.resolve()},"
            f"dst={CONTAINER_OUTPUT_DIRECTORY}"
        ),
        image_id,
        "--input",
        CONTAINER_INPUT_PATH,
        "--tier",
        tier,
        "--output",
        CONTAINER_OUTPUT_PATH,
    ]


def _best_effort_remove_container(docker: str, cidfile: pathlib.Path) -> None:
    try:
        container_id = cidfile.read_text(encoding="ascii").strip()
    except OSError:
        return
    if not container_id:
        return
    try:
        subprocess.run(
            [docker, "rm", "--force", container_id],
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _validate_output_directory(
    result: AttemptResult,
    output_directory: pathlib.Path,
) -> AttemptResult:
    try:
        entries = list(output_directory.iterdir())
    except OSError as exc:
        return AttemptResult(
            AttemptKind.INFRASTRUCTURE_FAILURE,
            f"로컬 출력 디렉터리를 검사할 수 없습니다: {exc}",
            measurement_elapsed_ns=result.measurement_elapsed_ns,
        )
    if result.kind is AttemptKind.VALID and [entry.name for entry in entries] != [
        pathlib.Path(CONTAINER_OUTPUT_PATH).name
    ]:
        return AttemptResult(
            AttemptKind.EXECUTION_FAILURE,
            "출력 디렉터리에는 submission.json 하나만 있어야 합니다.",
            returncode=result.returncode,
            measurement_elapsed_ns=result.measurement_elapsed_ns,
        )
    if result.kind is AttemptKind.VALID:
        try:
            mode = stat.S_IMODE(entries[0].stat().st_mode)
        except OSError as exc:
            return AttemptResult(
                AttemptKind.INFRASTRUCTURE_FAILURE,
                f"로컬 출력 파일 권한을 검사할 수 없습니다: {exc}",
                measurement_elapsed_ns=result.measurement_elapsed_ns,
            )
        if mode != 0o644:
            return AttemptResult(
                AttemptKind.EXECUTION_FAILURE,
                "submission.json 권한은 0644여야 합니다.",
                returncode=result.returncode,
                measurement_elapsed_ns=result.measurement_elapsed_ns,
            )
    return result


def _run_once(
    *,
    docker: str,
    image_id: str,
    input_path: pathlib.Path,
    inputs: Any,
    tier: str,
    target: pathlib.Path,
    repetition: int,
) -> AttemptResult:
    output_directory = target / f"{tier}-{repetition}"
    output_directory.mkdir(mode=0o700)
    # The parent remains private. The container's fixed UID needs write access
    # only to this local, non-adversarial check directory.
    output_directory.chmod(0o777)
    output_path = output_directory / pathlib.Path(CONTAINER_OUTPUT_PATH).name
    cidfile = target / f".{tier}-{repetition}.cid"
    container_name = (
        f"ossp-router-local-check-{os.getpid()}-{secrets.token_hex(6)}"
    )
    arguments = _container_arguments(
        docker=docker,
        image_id=image_id,
        input_path=input_path,
        output_directory=output_directory,
        tier=tier,
        cidfile=cidfile,
        container_name=container_name,
    )
    try:
        result = _run_prepared_router_once(
            arguments,
            inputs=inputs,
            policy=load_bundled_policy(),
            output_path=output_path,
            tier=tier,
            timeout_seconds=PHASE_C_CANDIDATE_LIMITS.wall_time_seconds,
        )
        return _validate_output_directory(result, output_directory)
    finally:
        _best_effort_remove_container(docker, cidfile)
        try:
            cidfile.unlink()
        except FileNotFoundError:
            pass


def _result_record(result: AttemptResult, repetition: int) -> Dict[str, Any]:
    elapsed_seconds = (
        None
        if result.measurement_elapsed_ns is None
        else result.measurement_elapsed_ns / 1_000_000_000
    )
    return {
        "repetition": repetition,
        "status": result.kind.value,
        "passed": result.kind is AttemptKind.VALID,
        "elapsed_seconds": elapsed_seconds,
        "limit_seconds": PHASE_C_CANDIDATE_LIMITS.wall_time_seconds,
        "output_bytes": result.output_bytes,
        "output_sha256": result.output_sha256,
        "detail": result.detail,
        "returncode": result.returncode,
        "timed_out": result.timed_out,
        "stderr_tail": result.stderr[-4_096:],
    }


def check_image(
    *,
    docker: str,
    requested_image: str,
    tiers: Sequence[str],
    repetitions: int,
    train_input: pathlib.Path,
    dev_input: pathlib.Path,
    registry: pathlib.Path,
) -> Dict[str, Any]:
    if repetitions < 1 or repetitions > 5:
        raise ValueError("반복 횟수는 1 이상 5 이하여야 합니다.")
    if not tiers or any(tier not in TIERS for tier in tiers):
        raise ValueError("하나 이상의 올바른 평가 등급이 필요합니다.")

    image_id = _resolve_image(docker, requested_image)
    runtime_platform = _runtime_platform(docker)
    public_inputs = load_public_runtime_workload(
        train_path=train_input,
        dev_path=dev_input,
        registry_path=registry,
    )
    with tempfile.TemporaryDirectory(
        prefix=".ossp-router-runtime-check-",
        dir=pathlib.Path.home(),
    ) as temporary:
        target = pathlib.Path(temporary)
        target.chmod(0o700)
        input_path = target / "inputs.json"
        write_json(input_path, input_batch_to_dict(public_inputs))
        input_path.chmod(0o644)
        input_bytes = input_path.read_bytes()
        inputs = load_input(input_path)
        tier_records = []
        for tier in tiers:
            results = [
                _result_record(
                    _run_once(
                        docker=docker,
                        image_id=image_id,
                        input_path=input_path,
                        inputs=inputs,
                        tier=tier,
                        target=target,
                        repetition=repetition,
                    ),
                    repetition,
                )
                for repetition in range(1, repetitions + 1)
            ]
            elapsed = [
                record["elapsed_seconds"]
                for record in results
                if record["elapsed_seconds"] is not None
            ]
            tier_records.append(
                {
                    "tier": tier,
                    "passed": all(record["passed"] for record in results),
                    "elapsed_seconds_max": max(elapsed) if elapsed else None,
                    "repetitions": results,
                }
            )

    limits = PHASE_C_CANDIDATE_LIMITS
    warnings = []
    if runtime_platform != OFFICIAL_CONTAINER_PLATFORM:
        warnings.append(
            "Docker 서버가 공식 linux/arm64 장비와 같지 않아 실행 시간은 "
            "참고값입니다."
        )
    warnings.append(
        "로컬 검사는 출력 파일을 사후 검증합니다. 공식 평가는 제한된 tmpfs "
        "출력 볼륨과 별도 보안 검사를 추가로 적용합니다."
    )
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "report_type": "participant-public-runtime-check",
        "workload": {
            "profile_id": PUBLIC_RUNTIME_PROFILE_ID,
            "episodes": len(public_inputs.episodes),
            "bytes": len(input_bytes),
            "sha256": hashlib.sha256(input_bytes).hexdigest(),
            "basis": "materialized-public-train-dev-inputs",
            "represents_evaluation_distribution": False,
        },
        "image": {
            "requested_reference": requested_image,
            "resolved_local_id": image_id,
            "platform": OFFICIAL_CONTAINER_PLATFORM,
        },
        "runtime": {
            "docker_server_platform": runtime_platform,
            "official_platform": OFFICIAL_CONTAINER_PLATFORM,
        },
        "limits": {
            "cpu_cores": limits.cpu_cores,
            "wall_time_seconds_per_tier": limits.wall_time_seconds,
            "memory_bytes": limits.memory_bytes,
            "memory_swap_total_bytes": limits.memory_swap_total_bytes,
            "processes_and_threads_total": limits.processes_and_threads_total,
            "temporary_storage_bytes": limits.temporary_storage_bytes,
            "output_bytes": limits.output_bytes,
        },
        "tiers": tier_records,
        "passed": all(record["passed"] for record in tier_records),
        "warnings": warnings,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "참가자 이미지를 공개 Train/Dev 입력과 공식 자원 한도로 "
            "로컬 검사합니다."
        )
    )
    parser.add_argument("--image", required=True, help="검사할 로컬 이미지")
    parser.add_argument(
        "--tier",
        action="append",
        choices=TIERS,
        dest="tiers",
        help="검사할 등급; 생략하면 세 등급을 모두 검사합니다.",
    )
    parser.add_argument(
        "--repetitions",
        type=int,
        default=1,
        help="등급별 반복 횟수(1~5, 기본 1)",
    )
    parser.add_argument(
        "--docker-command",
        default="docker",
        help="Docker 호환 CLI 이름 또는 경로",
    )
    parser.add_argument(
        "--report",
        type=pathlib.Path,
        help="선택적인 JSON 보고서 경로",
    )
    parser.add_argument(
        "--train-input",
        type=pathlib.Path,
        default=DEFAULT_TRAIN_INPUT,
        help="materialized 공개 Train 입력",
    )
    parser.add_argument(
        "--dev-input",
        type=pathlib.Path,
        default=DEFAULT_DEV_INPUT,
        help="materialized 공개 Dev 입력",
    )
    parser.add_argument(
        "--registry",
        type=pathlib.Path,
        default=DEFAULT_PUBLIC_DATA_REGISTRY,
        help="공개 자료 수량과 SHA-256 registry",
    )
    return parser


def _print_summary(report: Mapping[str, Any]) -> None:
    workload = report["workload"]
    print(
        "공개 Train+Dev 입력: "
        f"{workload['episodes']:,}문항, {workload['bytes']:,}바이트"
    )
    print(f"입력 SHA-256: {workload['sha256']}")
    for tier in report["tiers"]:
        status = "PASS" if tier["passed"] else "FAIL"
        elapsed = tier["elapsed_seconds_max"]
        elapsed_text = "측정 불가" if elapsed is None else f"{elapsed:.3f}초"
        print(
            f"{tier['tier']}: {status} ({elapsed_text} / "
            f"{report['limits']['wall_time_seconds_per_tier']}초)"
        )
        if not tier["passed"]:
            for repetition in tier["repetitions"]:
                if not repetition["passed"]:
                    print(
                        f"  반복 {repetition['repetition']}: "
                        f"{repetition['detail']}"
                    )
                    if repetition.get("stderr_tail"):
                        print(f"  Docker 오류: {repetition['stderr_tail'].strip()}")
    for warning in report["warnings"]:
        print(f"주의: {warning}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        docker = _resolve_docker(args.docker_command)
        report = check_image(
            docker=docker,
            requested_image=args.image,
            tiers=tuple(args.tiers or TIERS),
            repetitions=args.repetitions,
            train_input=args.train_input,
            dev_input=args.dev_input,
            registry=args.registry,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    _print_summary(report)
    if args.report is not None:
        try:
            write_json(args.report, report)
        except OSError as exc:
            print(f"error: 보고서를 쓸 수 없습니다: {exc}", file=sys.stderr)
            return 2
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
