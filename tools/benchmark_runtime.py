# SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
# SPDX-License-Identifier: Apache-2.0

"""Measure representative routers on materialized public Train/Dev inputs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import platform
import re
import resource
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ossp_router.heuristic import make_submission, write_submission_atomic
from ossp_router.orchestrator import (
    input_batch_to_dict,
    make_id_order_audit_variant,
)
from ossp_router.protocol import (
    TIERS,
    InputBatch,
    load_bundled_policy,
    load_input,
    load_submission,
    write_json,
)
from ossp_router.public_runtime import (
    PUBLIC_RUNTIME_PROFILE_ID,
    load_public_runtime_workload,
)
from ossp_router.runtime import (
    DEFAULT_TERMINATION_GRACE_SECONDS,
    PHASE_C_CANDIDATE_LIMITS,
    validate_runtime_submission,
)


ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_TRAIN_INPUT = ROOT / "data/materialized/train/inputs.json"
DEFAULT_DEV_INPUT = ROOT / "data/materialized/dev/inputs.json"
DEFAULT_PUBLIC_DATA_REGISTRY = ROOT / "data/public-data.v1.json"
STRATEGIES = (
    "always-light",
    "prompt-heuristic",
    "feature-budget",
    "hash-regex",
)
REPRESENTATIVE_STRATEGIES = frozenset(("feature-budget", "hash-regex"))
DEFAULT_REPETITIONS = 5
MEASUREMENT_MODES = ("local-screening", "apple-silicon-colima")
APPLE_SILICON_OPERATOR_ATTESTATION = "I_ATTEST_APPLE_SILICON_COLIMA"
FINAL_RUNTIME_BOUNDARY_ATTESTATION = "I_ATTEST_FINAL_RUNTIME_BOUNDARIES_PASSED"
DEFAULT_COLIMA_PROFILE = "default"
BENCHMARK_CLIENT_TIMEOUT_SECONDS = (
    PHASE_C_CANDIDATE_LIMITS.wall_time_seconds
    + int(DEFAULT_TERMINATION_GRACE_SECONDS)
    + 10
)
BASE_IMAGE_INDEX_DIGEST = (
    "sha256:f73754c398b259dfbbe482361dca8b464dea57da74efe5214966ca2ee767ee12"
)
SOURCE_MANIFEST_LABEL = "io.sktelecom.ossp.source-manifest-sha256"
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
CONTAINER_WORKER = """
import json
import resource
import sys
import threading
from pathlib import Path

from ossp_router.heuristic import make_submission, write_submission_atomic
from ossp_router.protocol import load_bundled_policy, load_input

sys.path.insert(0, "/opt/router/baselines")

def read_key_values(path):
    result = {}
    for line in Path(path).read_text(encoding="ascii").splitlines():
        fields = line.split()
        if len(fields) != 2:
            raise RuntimeError("unexpected cgroup key/value line")
        result[fields[0]] = int(fields[1])
    return result

def read_integer(path):
    return int(Path(path).read_text(encoding="ascii").strip())

cgroup_root = Path("/sys/fs/cgroup")
require_cgroup = sys.argv[5] == "require-cgroup-v2"
try:
    cpu_before = read_key_values(cgroup_root / "cpu.stat")
except (OSError, RuntimeError, ValueError):
    if require_cgroup:
        raise
    cpu_before = None
inputs = load_input(Path(sys.argv[3]))
policy = load_bundled_policy()
strategy = sys.argv[1]
if strategy == "feature-budget":
    from feature_budget import make_feature_budget_submission

    submission = make_feature_budget_submission(
        inputs, policy, sys.argv[2]
    ).submission
elif strategy == "hash-regex":
    from hash_regex import load_artifact, make_hash_regex_submission

    artifact = load_artifact(
        Path("/opt/router/baselines/hash-regex-public.v1.json")
    )
    submission = make_hash_regex_submission(
        inputs, policy, artifact, sys.argv[2]
    ).submission
else:
    submission = make_submission(inputs, policy, sys.argv[2], strategy=strategy)
write_submission_atomic(Path(sys.argv[4]), submission)
usage = resource.getrusage(resource.RUSAGE_SELF)
try:
    cpu_after = read_key_values(cgroup_root / "cpu.stat")
    cgroup_metrics = {
        "cpu_stat": cpu_after,
        "cpu_usage_delta_usec": (
            cpu_after["usage_usec"] - cpu_before["usage_usec"]
            if cpu_before is not None else None
        ),
        "memory_peak_bytes": read_integer(cgroup_root / "memory.peak"),
        "memory_events": read_key_values(cgroup_root / "memory.events"),
        "pids_peak": read_integer(cgroup_root / "pids.peak"),
        "pids_current": read_integer(cgroup_root / "pids.current"),
        "pids_events": read_key_values(cgroup_root / "pids.events"),
    }
except (OSError, RuntimeError, ValueError, KeyError):
    if require_cgroup:
        raise
    cgroup_metrics = None
print(json.dumps({
    "cpu_seconds": usage.ru_utime + usage.ru_stime,
    "max_rss_bytes": usage.ru_maxrss * 1024,
    "processes": 1,
    "threads": threading.active_count(),
    "cgroup_v2": cgroup_metrics,
}, sort_keys=True, separators=(",", ":")))
"""


def _rss_bytes(raw_value: int) -> int:
    # macOS reports bytes; Linux and the BSDs used in CI report KiB.
    if sys.platform == "darwin":
        return raw_value
    return raw_value * 1024


def _worker(args: argparse.Namespace) -> int:
    inputs = load_input(args.input)
    policy = load_bundled_policy()
    if args.strategy in REPRESENTATIVE_STRATEGIES:
        baseline_path = str(ROOT / "baselines")
        if baseline_path not in sys.path:
            sys.path.insert(0, baseline_path)
        if args.strategy == "feature-budget":
            from feature_budget import make_feature_budget_submission

            submission = make_feature_budget_submission(
                inputs, policy, args.tier
            ).submission
        else:
            from hash_regex import load_artifact, make_hash_regex_submission

            artifact = load_artifact(
                ROOT / "baselines/hash-regex-public.v1.json"
            )
            submission = make_hash_regex_submission(
                inputs, policy, artifact, args.tier
            ).submission
    else:
        submission = make_submission(
            inputs,
            policy,
            args.tier,
            strategy=args.strategy,
        )
    write_submission_atomic(args.output, submission)
    usage = resource.getrusage(resource.RUSAGE_SELF)
    metrics = {
        "cpu_seconds": usage.ru_utime + usage.ru_stime,
        "max_rss_bytes": _rss_bytes(usage.ru_maxrss),
        "processes": 1,
        "threads": threading.active_count(),
    }
    print(json.dumps(metrics, sort_keys=True, separators=(",", ":")))
    return 0


def _environment() -> Dict[str, str]:
    return {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
    }


def _sha256_file(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_tree_manifest() -> Dict[str, Any]:
    roots = (
        ROOT / "container/Dockerfile",
        ROOT / "container/measurement.Dockerfile",
        ROOT / "container/entrypoint.py",
        ROOT / "src",
        ROOT / "baselines/feature_budget.py",
        ROOT / "baselines/hash_regex.py",
        ROOT / "baselines/hash-regex-public.v1.json",
    )
    files: List[pathlib.Path] = []
    for root in roots:
        if root.is_symlink():
            raise RuntimeError(
                f"소스 파일 목록에 심볼릭 링크를 포함할 수 없습니다: {root}"
            )
        if root.is_file():
            files.append(root)
        elif root.is_dir():
            for path in root.rglob("*"):
                if path.is_symlink():
                    raise RuntimeError(
                        "소스 파일 목록에 심볼릭 링크를 포함할 수 없습니다: "
                        f"{path}"
                    )
                if (
                    path.is_file()
                    and "__pycache__" not in path.parts
                    and not any(part.endswith(".egg-info") for part in path.parts)
                    and not path.name.endswith((".pyc", ".pyo"))
                ):
                    files.append(path)
    entries = [
        {
            "path": path.relative_to(ROOT).as_posix(),
            "sha256": _sha256_file(path),
        }
        for path in sorted(set(files))
    ]
    encoded = json.dumps(
        entries,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "algorithm": "sha256",
        "scope": [
            "container/Dockerfile",
            "container/measurement.Dockerfile",
            "container/entrypoint.py",
            "src",
            "baselines/feature_budget.py",
            "baselines/hash_regex.py",
            "baselines/hash-regex-public.v1.json",
        ],
        "entries": entries,
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _artifact_hashes() -> Dict[str, str]:
    return {
        "benchmark_tool_sha256": _sha256_file(pathlib.Path(__file__).resolve()),
        "dockerfile_sha256": _sha256_file(ROOT / "container/Dockerfile"),
        "measurement_dockerfile_sha256": _sha256_file(
            ROOT / "container/measurement.Dockerfile"
        ),
        "policy_sha256": _sha256_file(
            ROOT / "src/ossp_router/resources/routing-policy.v1.json"
        ),
        "representative_router_artifact_sha256": _sha256_file(
            ROOT / "baselines/hash-regex-public.v1.json"
        ),
    }


def _docker_json(
    docker: str,
    command: Sequence[str],
    *,
    description: str,
) -> Mapping[str, Any]:
    completed = subprocess.run(
        [docker, *command],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"{description} 실패: {completed.stderr.strip()}")
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{description} 결과가 JSON이 아닙니다.") from exc
    if not isinstance(value, Mapping):
        raise RuntimeError(f"{description} 결과가 JSON 객체가 아닙니다.")
    return value


def _local_docker_context_evidence(
    docker: str,
    docker_info: Mapping[str, Any],
) -> Dict[str, str]:
    """Bind host CPU evidence to a local Unix-socket Docker daemon."""

    docker_host = os.environ.get("DOCKER_HOST")
    if docker_host:
        context_name = "DOCKER_HOST"
        endpoint = docker_host
    else:
        shown = subprocess.run(
            [docker, "context", "show"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        if shown.returncode != 0:
            raise RuntimeError(
                "Docker context를 확인할 수 없습니다: "
                f"{shown.stderr.strip()}"
            )
        context_name = shown.stdout.strip()
        if (
            not context_name
            or len(context_name) > 128
            or any(ord(character) < 0x20 for character in context_name)
        ):
            raise RuntimeError("Docker context 이름이 올바르지 않습니다.")
        inspected = subprocess.run(
            [
                docker,
                "context",
                "inspect",
                context_name,
                "--format",
                "{{json .Endpoints.docker.Host}}",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        if inspected.returncode != 0:
            raise RuntimeError(
                "Docker context endpoint를 확인할 수 없습니다: "
                f"{inspected.stderr.strip()}"
            )
        try:
            endpoint = json.loads(inspected.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "Docker context endpoint 결과가 JSON 문자열이 아닙니다."
            ) from exc
    if (
        not isinstance(endpoint, str)
        or not endpoint.startswith("unix://")
        or not pathlib.PurePosixPath(endpoint.removeprefix("unix://")).is_absolute()
    ):
        raise RuntimeError(
            "공식 Apple Silicon 측정은 로컬 Unix socket Docker "
            "context만 허용합니다. TCP·SSH 원격 daemon은 사용할 수 없습니다."
        )
    daemon_name = docker_info.get("Name")
    if not isinstance(daemon_name, str) or not daemon_name:
        raise RuntimeError("Docker daemon 이름이 없습니다.")
    return {
        "context": context_name,
        "endpoint_type": "local-unix-socket",
        "daemon_name": daemon_name,
        "virtualization_boundary": "local-colima-linux-vm",
    }


def _resolve_container_image(docker: str, reference: str) -> Dict[str, Any]:
    inspected = subprocess.run(
        [docker, "image", "inspect", reference],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    if inspected.returncode != 0:
        raise RuntimeError(
            f"로컬 컨테이너 이미지 확인 실패: {inspected.stderr.strip()}"
        )
    try:
        values = json.loads(inspected.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("컨테이너 이미지 inspect 결과가 JSON이 아닙니다.") from exc
    if not isinstance(values, list) or len(values) != 1:
        raise RuntimeError("컨테이너 이미지 inspect 결과는 정확히 하나여야 합니다.")
    value = values[0]
    if not isinstance(value, Mapping):
        raise RuntimeError("컨테이너 이미지 inspect 항목이 JSON 객체가 아닙니다.")
    image_id = value.get("Id")
    architecture = value.get("Architecture")
    operating_system = value.get("Os")
    if (
        not isinstance(image_id, str)
        or not image_id.startswith("sha256:")
        or len(image_id) != 71
    ):
        raise RuntimeError("컨테이너 이미지의 로컬 content ID가 올바르지 않습니다.")
    if not isinstance(architecture, str) or not isinstance(operating_system, str):
        raise RuntimeError("컨테이너 이미지 플랫폼 정보가 없습니다.")
    config = value.get("Config")
    labels = config.get("Labels") if isinstance(config, Mapping) else None
    source_manifest_sha256 = (
        labels.get(SOURCE_MANIFEST_LABEL)
        if isinstance(labels, Mapping)
        else None
    )
    return {
        "requested_reference": reference,
        "resolved_local_content_id": image_id,
        "config_digest": image_id,
        "platform": {
            "os": operating_system,
            "architecture": architecture,
        },
        "repo_digests": sorted(value.get("RepoDigests") or []),
        "size_bytes": value.get("Size"),
        "source_manifest_sha256": source_manifest_sha256,
    }


def _require_image_source_binding(
    image: Mapping[str, Any],
    *,
    current_source_manifest_sha256: str,
) -> None:
    """Reject an image that was not built from the currently measured tree."""

    image_digest = image.get("source_manifest_sha256")
    if (
        not isinstance(image_digest, str)
        or SHA256_RE.fullmatch(image_digest) is None
    ):
        raise RuntimeError(
            "측정 이미지에 현재 소스 파일 목록과 결속하는 SHA-256 OCI "
            "label이 없습니다. SOURCE_MANIFEST_SHA256 build arg로 "
            "이미지를 다시 빌드해야 합니다."
        )
    if image_digest != current_source_manifest_sha256:
        raise RuntimeError(
            "측정 이미지의 소스 파일 목록 SHA-256이 현재 공개 소스 트리와 "
            "다릅니다. stale 이미지는 측정할 수 없습니다."
        )


def _probe_apple_silicon_environment(
    *,
    docker: Optional[str],
    colima: Optional[str],
    colima_profile: str,
    attestation: Optional[str],
    environment_label: Optional[str],
) -> Dict[str, Any]:
    if attestation != APPLE_SILICON_OPERATOR_ATTESTATION:
        raise RuntimeError(
            "공식 Apple Silicon 측정에는 "
            "--apple-silicon-operator-attestation "
            f"{APPLE_SILICON_OPERATOR_ATTESTATION} 확인이 필요합니다."
        )
    if (
        environment_label is None
        or not environment_label.strip()
        or environment_label != environment_label.strip()
        or len(environment_label) > 128
        or any(ord(character) < 0x20 for character in environment_label)
    ):
        raise RuntimeError(
            "공식 Apple Silicon 측정에는 앞뒤 공백 없는 1~128자 "
            "--apple-silicon-environment-label이 필요합니다."
        )
    if platform.system() != "Darwin" or platform.machine().lower() not in {
        "arm64",
        "aarch64",
    }:
        raise RuntimeError(
            "공식 측정은 Apple Silicon macOS 호스트에서만 허용됩니다."
        )
    if docker is None:
        raise RuntimeError(
            "공식 Apple Silicon 측정에는 Docker CLI와 실행 중인 daemon이 "
            "필요합니다."
        )
    if colima is None:
        raise RuntimeError(
            "공식 Apple Silicon 측정에는 Colima CLI가 필요합니다."
        )
    completed = subprocess.run(
        [colima, "status", colima_profile, "--json"],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "Colima profile 상태 확인 실패: " + completed.stderr.strip()
        )
    try:
        colima_status = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Colima 상태 결과가 JSON이 아닙니다.") from exc
    if not isinstance(colima_status, Mapping):
        raise RuntimeError("Colima 상태 결과가 JSON 객체가 아닙니다.")
    colima_arch = str(colima_status.get("arch") or "").lower()
    colima_runtime = str(colima_status.get("runtime") or "").lower()
    if colima_arch not in {"arm64", "aarch64"} or colima_runtime != "docker":
        raise RuntimeError(
            "Colima profile은 aarch64와 Docker runtime을 사용해야 합니다."
        )
    docker_info = _docker_json(
        docker,
        ["info", "--format", "{{json .}}"],
        description="Docker server 정보 확인",
    )
    server_os = str(docker_info.get("OSType") or "").lower()
    server_arch = str(docker_info.get("Architecture") or "").lower()
    cgroup_version = str(docker_info.get("CgroupVersion") or "")
    if server_os != "linux" or server_arch not in {"arm64", "aarch64"}:
        raise RuntimeError("Docker server가 linux/arm64가 아닙니다.")
    if cgroup_version != "2":
        raise RuntimeError("공식 Apple Silicon 측정에는 cgroup v2가 필요합니다.")
    context = _local_docker_context_evidence(docker, docker_info)
    return {
        "attestation": APPLE_SILICON_OPERATOR_ATTESTATION,
        "environment_label": environment_label,
        "host": {
            "system": platform.system(),
            "machine": platform.machine(),
            "release": platform.release(),
        },
        "colima": {
            "profile": colima_profile,
            "architecture": colima_arch,
            "cpus": colima_status.get("cpu", colima_status.get("cpus")),
            "memory_bytes": colima_status.get("memory"),
            "disk_bytes": colima_status.get("disk"),
            "runtime": colima_runtime,
        },
        "docker": {
            "operating_system": server_os,
            "architecture": server_arch,
            "cgroup_version": cgroup_version,
            "cgroup_driver": docker_info.get("CgroupDriver"),
            "storage_driver": docker_info.get("Driver"),
            "server_version": docker_info.get("ServerVersion"),
            "local_context": context,
        },
    }


def _validate_cgroup_v2_metrics(metrics: Mapping[str, Any]) -> Dict[str, Any]:
    cgroup = metrics.get("cgroup_v2")
    if not isinstance(cgroup, Mapping):
        raise RuntimeError("컨테이너 측정값에 cgroup v2 정보가 없습니다.")
    cpu_stat = cgroup.get("cpu_stat")
    memory_events = cgroup.get("memory_events")
    pids_events = cgroup.get("pids_events")
    required_cpu_stat = {
        "usage_usec",
        "user_usec",
        "system_usec",
        "nr_periods",
        "nr_throttled",
        "throttled_usec",
    }
    if (
        not isinstance(cpu_stat, Mapping)
        or not required_cpu_stat.issubset(cpu_stat)
        or any(
            not isinstance(cpu_stat.get(key), int) or cpu_stat[key] < 0
            for key in required_cpu_stat
        )
    ):
        raise RuntimeError("cgroup cpu.stat 필수 값이 없습니다.")
    required_integers = (
        "cpu_usage_delta_usec",
        "memory_peak_bytes",
        "pids_peak",
        "pids_current",
    )
    if any(
        not isinstance(cgroup.get(key), int) or cgroup[key] < 0
        for key in required_integers
    ):
        raise RuntimeError("cgroup v2 정수 측정값이 없거나 올바르지 않습니다.")
    if not isinstance(memory_events, Mapping) or not {
        "max",
        "oom",
        "oom_kill",
    }.issubset(memory_events):
        raise RuntimeError("cgroup memory.events 필수 값이 없습니다.")
    if not isinstance(pids_events, Mapping) or "max" not in pids_events:
        raise RuntimeError("cgroup pids.events 필수 값이 없습니다.")
    checked_events = (
        *(memory_events[key] for key in ("max", "oom", "oom_kill")),
        pids_events["max"],
    )
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in checked_events
    ):
        raise RuntimeError("cgroup event 측정값이 0 이상의 정수가 아닙니다.")
    if any(value != 0 for value in checked_events):
        raise RuntimeError(
            "공식 측정 컨테이너가 메모리 또는 PID 후보 한도에 "
            "도달했으므로 완료 근거로 사용할 수 없습니다."
        )
    return dict(cgroup)


def _worker_environment() -> Dict[str, str]:
    environment = dict(os.environ)
    existing = environment.get("PYTHONPATH")
    source_path = str(ROOT / "src")
    environment["PYTHONPATH"] = (
        source_path if not existing else os.pathsep.join((source_path, existing))
    )
    return environment


def _run_once(
    *,
    strategy: str,
    tier: str,
    input_path: pathlib.Path,
    output_path: pathlib.Path,
    inputs: InputBatch,
) -> Dict[str, Any]:
    command = [
        sys.executable,
        str(pathlib.Path(__file__).resolve()),
        "--worker",
        "--strategy",
        strategy,
        "--tier",
        tier,
        "--input",
        str(input_path),
        "--output",
        str(output_path),
    ]
    started = time.perf_counter()
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=_worker_environment(),
        capture_output=True,
        text=True,
        check=False,
        timeout=BENCHMARK_CLIENT_TIMEOUT_SECONDS,
    )
    elapsed = time.perf_counter() - started
    if completed.returncode != 0:
        raise RuntimeError(
            f"{strategy}/{tier} 측정 실패: {completed.stderr.strip()}"
        )
    try:
        worker_metrics = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"{strategy}/{tier} 측정값을 읽을 수 없습니다: {completed.stdout!r}"
        ) from exc
    submission = load_submission(output_path)
    policy = load_bundled_policy()
    validate_runtime_submission(inputs, submission, policy, tier)
    output_bytes = output_path.read_bytes()
    return {
        "elapsed_seconds": elapsed,
        "cpu_seconds": worker_metrics["cpu_seconds"],
        "max_rss_bytes": worker_metrics["max_rss_bytes"],
        "processes": worker_metrics["processes"],
        "threads": worker_metrics["threads"],
        "output_bytes": len(output_bytes),
        "output_sha256": hashlib.sha256(output_bytes).hexdigest(),
    }


def _run_container_once(
    *,
    docker: str,
    image: str,
    strategy: str,
    tier: str,
    input_path: pathlib.Path,
    output_path: pathlib.Path,
    inputs: InputBatch,
    require_official_cgroup: bool = False,
) -> Dict[str, Any]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.parent.chmod(0o777)
    descriptor, cidfile_name = tempfile.mkstemp(prefix=".ossp-router-benchmark-")
    os.close(descriptor)
    cidfile = pathlib.Path(cidfile_name)
    cidfile.unlink()
    command = [
        docker,
        "run",
        "--rm",
        "--pull",
        "never",
        "--cidfile",
        str(cidfile),
        "--stop-timeout",
        str(int(DEFAULT_TERMINATION_GRACE_SECONDS)),
        "--network",
        "none",
        "--cgroupns",
        "private",
        "--ipc",
        "none",
        "--log-driver",
        "none",
        "--ulimit",
        "core=0:0",
        "--read-only",
        "--user",
        "65532:65532",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--cpus",
        str(PHASE_C_CANDIDATE_LIMITS.cpu_cores),
        "--memory",
        f"{PHASE_C_CANDIDATE_LIMITS.memory_bytes // (1024 * 1024)}m",
        "--memory-swap",
        (
            f"{PHASE_C_CANDIDATE_LIMITS.memory_swap_total_bytes // (1024 * 1024)}m"
        ),
        "--pids-limit",
        str(PHASE_C_CANDIDATE_LIMITS.processes_and_threads_total),
        "--tmpfs",
        (
            "/tmp:rw,noexec,nosuid,size="
            f"{PHASE_C_CANDIDATE_LIMITS.temporary_storage_bytes // (1024 * 1024)}m"
        ),
        "--mount",
        (
            f"type=bind,src={input_path.resolve()},"
            "dst=/challenge/input/inputs.json,readonly"
        ),
        "--mount",
        (
            f"type=bind,src={output_path.parent.resolve()},"
            "dst=/challenge/output"
        ),
        "--entrypoint",
        "python3",
        image,
        "-c",
        CONTAINER_WORKER,
        strategy,
        tier,
        "/challenge/input/inputs.json",
        f"/challenge/output/{output_path.name}",
        "require-cgroup-v2" if require_official_cgroup else "optional-cgroup-v2",
    ]
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=BENCHMARK_CLIENT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        cleanup_detail = ""
        try:
            container_id = cidfile.read_text(encoding="utf-8").strip()
        except OSError:
            container_id = ""
        if container_id:
            try:
                stopped = subprocess.run(
                    [
                        docker,
                        "stop",
                        "--timeout",
                        str(int(DEFAULT_TERMINATION_GRACE_SECONDS)),
                        container_id,
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=DEFAULT_TERMINATION_GRACE_SECONDS + 5,
                )
            except (OSError, subprocess.TimeoutExpired):
                stopped = None
            if stopped is None or stopped.returncode != 0:
                try:
                    removed = subprocess.run(
                        [docker, "rm", "--force", container_id],
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=5,
                    )
                except (OSError, subprocess.TimeoutExpired) as cleanup_exc:
                    cleanup_detail = f"; 컨테이너 정리 실패: {cleanup_exc}"
                else:
                    if removed.returncode != 0:
                        cleanup_detail = (
                            f"; 컨테이너 정리 실패: {removed.stderr.strip()}"
                        )
        raise RuntimeError(
            f"{strategy}/{tier} 컨테이너 측정 시간 초과{cleanup_detail}"
        ) from exc
    finally:
        try:
            cidfile.unlink()
        except FileNotFoundError:
            pass
    elapsed = time.perf_counter() - started
    if completed.returncode != 0:
        raise RuntimeError(
            f"{strategy}/{tier} 컨테이너 측정 실패: {completed.stderr.strip()}"
        )
    try:
        worker_metrics = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"{strategy}/{tier} 컨테이너 측정값을 읽을 수 없습니다: "
            f"{completed.stdout!r}"
        ) from exc
    submission = load_submission(output_path)
    policy = load_bundled_policy()
    validate_runtime_submission(inputs, submission, policy, tier)
    output_bytes = output_path.read_bytes()
    result = {
        "elapsed_seconds": elapsed,
        "cpu_seconds": worker_metrics["cpu_seconds"],
        "max_rss_bytes": worker_metrics["max_rss_bytes"],
        "processes": worker_metrics["processes"],
        "threads": worker_metrics["threads"],
        "output_bytes": len(output_bytes),
        "output_sha256": hashlib.sha256(output_bytes).hexdigest(),
        "container_argv": _public_container_argv(command),
    }
    if require_official_cgroup:
        result["cgroup_v2"] = _validate_cgroup_v2_metrics(worker_metrics)
    elif isinstance(worker_metrics.get("cgroup_v2"), Mapping):
        result["cgroup_v2"] = dict(worker_metrics["cgroup_v2"])
    else:
        result["cgroup_v2"] = None
    return result


def _public_container_argv(command: Sequence[str]) -> List[str]:
    """Remove host-specific executable and mount paths from report metadata."""

    public = list(command)
    if public:
        public[0] = "<docker-cli>"
    for index, argument in enumerate(public):
        if index > 0 and public[index - 1] == "--cidfile":
            public[index] = "<operator-cidfile>"
        elif (
            argument.startswith("type=bind,src=")
            and ",dst=/challenge/input/inputs.json," in argument
        ):
            public[index] = (
                "type=bind,src=<public-runtime-input>,"
                "dst=/challenge/input/inputs.json,readonly"
            )
        elif (
            argument.startswith("type=bind,src=")
            and ",dst=/challenge/output" in argument
        ):
            public[index] = (
                "type=bind,src=<benchmark-output>,dst=/challenge/output"
            )
    return public


def _mapped_decisions_match(
    original_submission_path: pathlib.Path,
    variant_submission_path: pathlib.Path,
    id_mapping: Sequence[Tuple[str, str]],
) -> bool:
    original = load_submission(original_submission_path)
    variant = load_submission(variant_submission_path)
    original_models = {
        decision.episode_id: decision.model_id
        for decision in original.decisions
    }
    variant_models = {
        decision.episode_id: decision.model_id
        for decision in variant.decisions
    }
    return all(
        original_models[original_id] == variant_models[audit_id]
        for original_id, audit_id in id_mapping
    )


def _audit_invariance(
    *,
    strategy: str,
    tier: str,
    original_path: pathlib.Path,
    original_inputs: InputBatch,
    variant_path: pathlib.Path,
    variant_inputs: InputBatch,
    target: pathlib.Path,
    id_mapping: Sequence[Tuple[str, str]],
) -> bool:
    original_output = target / f"audit-original-{strategy}-{tier}.json"
    variant_output = target / f"audit-variant-{strategy}-{tier}.json"
    _run_once(
        strategy=strategy,
        tier=tier,
        input_path=original_path,
        output_path=original_output,
        inputs=original_inputs,
    )
    _run_once(
        strategy=strategy,
        tier=tier,
        input_path=variant_path,
        output_path=variant_output,
        inputs=variant_inputs,
    )
    return _mapped_decisions_match(
        original_output,
        variant_output,
        id_mapping,
    )


def _audit_container_invariance(
    *,
    docker: str,
    image: str,
    strategy: str,
    tier: str,
    original_path: pathlib.Path,
    original_inputs: InputBatch,
    variant_path: pathlib.Path,
    variant_inputs: InputBatch,
    target: pathlib.Path,
    id_mapping: Sequence[Tuple[str, str]],
    require_official_cgroup: bool = False,
) -> bool:
    original_output = target / f"audit-original-{strategy}-{tier}.json"
    variant_output = target / f"audit-variant-{strategy}-{tier}.json"
    _run_container_once(
        docker=docker,
        image=image,
        strategy=strategy,
        tier=tier,
        input_path=original_path,
        output_path=original_output,
        inputs=original_inputs,
        require_official_cgroup=require_official_cgroup,
    )
    _run_container_once(
        docker=docker,
        image=image,
        strategy=strategy,
        tier=tier,
        input_path=variant_path,
        output_path=variant_output,
        inputs=variant_inputs,
        require_official_cgroup=require_official_cgroup,
    )
    return _mapped_decisions_match(
        original_output,
        variant_output,
        id_mapping,
    )


def _container_measurement(
    image: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    image_id = (
        str(image["resolved_local_content_id"])
        if image is not None
        else None
    )
    measurement: Dict[str, Any] = {
        "runtime_available": False,
        "runtime_command": None,
        "runtime_context": None,
        "runtime_server": None,
        "image_reference": (
            image.get("requested_reference") if image is not None else None
        ),
        "resolved_local_content_id": image_id,
        "image_size_bytes": None,
        "image_list_size_display": None,
        "runtime_rootfs_apparent_bytes": None,
        "runtime_rootfs_apparent_is_lower_bound": None,
        "base_image_index_digest": BASE_IMAGE_INDEX_DIGEST,
    }
    docker = shutil.which("docker")
    if docker is None:
        return measurement
    available = subprocess.run(
        [docker, "info"],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    if available.returncode != 0:
        return measurement
    measurement["runtime_available"] = True
    measurement["runtime_command"] = "<docker-cli>"
    context = subprocess.run(
        [docker, "context", "show"],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    if context.returncode == 0:
        measurement["runtime_context"] = context.stdout.strip()
    server = subprocess.run(
        [docker, "version", "--format", "{{json .Server}}"],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    if server.returncode == 0:
        try:
            measurement["runtime_server"] = json.loads(server.stdout)
        except json.JSONDecodeError:
            measurement["runtime_server"] = None
    if image_id is None:
        return measurement
    inspected = subprocess.run(
        [docker, "image", "inspect", "--format", "{{.Size}}", image_id],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    if inspected.returncode != 0:
        raise RuntimeError(f"컨테이너 이미지 크기 확인 실패: {inspected.stderr}")
    measurement["image_size_bytes"] = int(inspected.stdout.strip())
    listed = subprocess.run(
        [docker, "image", "ls", image_id, "--format", "{{json .}}"],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    if listed.returncode == 0 and listed.stdout.strip():
        try:
            measurement["image_list_size_display"] = json.loads(
                listed.stdout.splitlines()[0]
            ).get("Size")
        except json.JSONDecodeError:
            measurement["image_list_size_display"] = None
    rootfs = subprocess.run(
        [
            docker,
            "run",
            "--rm",
            "--pull",
            "never",
            "--network",
            "none",
            "--ipc",
            "none",
            "--log-driver",
            "none",
            "--ulimit",
            "core=0:0",
            "--read-only",
            "--user",
            "0:0",
            "--security-opt",
            "no-new-privileges",
            "--entrypoint",
            "du",
            image_id,
            "-sbx",
            "/",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=BENCHMARK_CLIENT_TIMEOUT_SECONDS,
    )
    rootfs_lines = [line for line in rootfs.stdout.splitlines() if line.strip()]
    if not rootfs_lines:
        raise RuntimeError(
            f"컨테이너 루트 파일 시스템 크기 확인 실패: {rootfs.stderr}"
        )
    measurement["runtime_rootfs_apparent_bytes"] = int(
        rootfs_lines[-1].split(maxsplit=1)[0]
    )
    measurement["runtime_rootfs_apparent_is_lower_bound"] = bool(
        rootfs.returncode != 0 or rootfs.stderr.strip()
    )
    return measurement


def _measurement_profile(container_measured: bool) -> Dict[str, Any]:
    limits = PHASE_C_CANDIDATE_LIMITS
    return {
        "host": {
            "client_timeout_seconds": BENCHMARK_CLIENT_TIMEOUT_SECONDS,
        },
        "container": (
            {
                "client_timeout_seconds": BENCHMARK_CLIENT_TIMEOUT_SECONDS,
                "cpu_cores": limits.cpu_cores,
                "memory_bytes": limits.memory_bytes,
                "memory_swap_total_bytes": limits.memory_swap_total_bytes,
                "network": "none",
                "cgroup_namespace": "private",
                "ipc": "none",
                "log_driver": "none",
                "core_ulimit": 0,
                "pids_limit": limits.processes_and_threads_total,
                "read_only_root": True,
                "temporary_storage_bytes": limits.temporary_storage_bytes,
                "user": "65532:65532",
                "output_storage": "host-bind-not-final-equivalent",
            }
            if container_measured
            else None
        ),
    }


def _proposal(
    results: Sequence[Mapping[str, Any]],
    container_results: Sequence[Mapping[str, Any]],
    container: Mapping[str, Any],
    *,
    apple_silicon_colima_completed: bool,
    final_runtime_boundary_validation_completed: bool = False,
) -> Dict[str, Any]:
    measured_results = (
        list(container_results)
        if apple_silicon_colima_completed
        else [*results, *container_results]
    )
    if not measured_results:
        raise RuntimeError("자원 제안에 사용할 baseline 측정값이 없습니다.")
    max_elapsed = max(
        repetition["elapsed_seconds"]
        for result in measured_results
        for repetition in result["repetitions"]
    )
    max_rss = max(
        repetition["max_rss_bytes"]
        for result in measured_results
        for repetition in result["repetitions"]
    )
    max_output = max(
        repetition["output_bytes"]
        for result in measured_results
        for repetition in result["repetitions"]
    )
    cgroup_measurements = [
        repetition["cgroup_v2"]
        for result in container_results
        for repetition in result["repetitions"]
        if isinstance(repetition.get("cgroup_v2"), Mapping)
    ]
    cgroup_observed_max = (
        {
            "cpu_usage_delta_usec": max(
                item["cpu_usage_delta_usec"] for item in cgroup_measurements
            ),
            "cpu_usage_usec": max(
                item["cpu_stat"]["usage_usec"]
                for item in cgroup_measurements
            ),
            "cpu_nr_throttled": max(
                item["cpu_stat"]["nr_throttled"]
                for item in cgroup_measurements
            ),
            "cpu_throttled_usec": max(
                item["cpu_stat"]["throttled_usec"]
                for item in cgroup_measurements
            ),
            "memory_peak_bytes": max(
                item["memory_peak_bytes"] for item in cgroup_measurements
            ),
            "pids_peak": max(
                item["pids_peak"] for item in cgroup_measurements
            ),
        }
        if cgroup_measurements
        else None
    )
    if apple_silicon_colima_completed and cgroup_observed_max is None:
        raise RuntimeError(
            "공식 Apple Silicon 제안에는 cgroup v2 반복 측정값이 필요합니다."
        )
    limits = PHASE_C_CANDIDATE_LIMITS
    measured_container_strategies = {
        result.get("strategy") for result in container_results
    }
    representative_router_validation_completed = (
        apple_silicon_colima_completed
        and REPRESENTATIVE_STRATEGIES.issubset(measured_container_strategies)
        and all(
            result.get("deterministic") is True
            and result.get("id_and_order_invariant") is True
            for result in container_results
            if result.get("strategy") in REPRESENTATIVE_STRATEGIES
        )
    )
    final_limits_frozen = (
        representative_router_validation_completed
        and final_runtime_boundary_validation_completed
    )
    runtime_server = container.get("runtime_server") or {}
    runtime_context = container.get("runtime_context") or "unknown"
    runtime_os = runtime_server.get("Os") or "unknown"
    runtime_arch = runtime_server.get("Arch") or "unknown"
    container_limitation = (
        "컨테이너 실행기가 없어 이미지 크기와 격리 오버헤드를 측정하지 못했습니다."
        if not container["runtime_available"]
        else (
            f"컨테이너 결과는 Docker context `{runtime_context}`의 "
            f"{runtime_os}/{runtime_arch} 측정이며 대표 참가자 구현의 "
            "성능을 대신하지는 않습니다."
            if container_results
            else (
                "이미지 크기는 기록했지만 baseline 시간과 RSS는 호스트 "
                "Python 실행값이므로 컨테이너 오버헤드를 포함하지 않습니다."
            )
        )
    )
    return {
        "status": (
            "final-frozen"
            if final_limits_frozen
            else (
                "provisional-awaiting-final-runtime-boundary-validation"
                if representative_router_validation_completed
                else "provisional-awaiting-representative-router-validation"
            )
        ),
        "scope": "per-tier-container-run",
        "cpu_cores": limits.cpu_cores,
        "wall_time_seconds": limits.wall_time_seconds,
        "memory_bytes": limits.memory_bytes,
        "memory_swap_total_bytes": limits.memory_swap_total_bytes,
        "processes_and_threads_total": limits.processes_and_threads_total,
        "output_bytes": limits.output_bytes,
        "output_inodes": limits.output_inodes,
        "retained_log_bytes_per_stream": limits.retained_log_bytes_per_stream,
        "emitted_log_bytes_per_stream": limits.emitted_log_bytes_per_stream,
        "temporary_storage_bytes": limits.temporary_storage_bytes,
        "oci_compressed_image_limit_bytes": limits.oci_compressed_image_bytes,
        "rootfs_apparent_limit_bytes": limits.rootfs_apparent_bytes,
        "platform_proposal": {
            "os": "linux",
            "architecture": "arm64",
            "status": "final" if final_limits_frozen else "provisional-not-final",
            "apple_silicon_colima_benchmark_completed": (
                apple_silicon_colima_completed
            ),
        },
        "validation": {
            "apple_silicon_colima_completed": apple_silicon_colima_completed,
            "representative_router_validation_completed": (
                representative_router_validation_completed
            ),
            "final_runtime_boundary_validation_completed": (
                final_runtime_boundary_validation_completed
            ),
        },
        "reference_router_observed_max": {
            "source": (
                "container-only"
                if apple_silicon_colima_completed
                else "host-and-container-combined"
            ),
            "elapsed_seconds": max_elapsed,
            "max_rss_bytes": max_rss,
            "output_bytes": max_output,
            "elapsed_rss_output_within_candidate_profile": (
                max_elapsed <= limits.wall_time_seconds
                and max_rss <= limits.memory_bytes
                and max_output <= limits.output_bytes
            ),
            "cgroup_v2_observed_max": cgroup_observed_max,
            "not_measured_by_process_worker": (
                [
                    "temporary storage peak",
                    "official output tmpfs peak and inode peak",
                    "OCI compressed layer sum",
                ]
                if apple_silicon_colima_completed
                else [
                    "cgroup memory.peak",
                    "container process and thread peak",
                    "temporary storage peak",
                    "OCI compressed layer sum",
                ]
            ),
        },
        "headroom_basis": (
            "대표 라우터 측정과 공식 출력·이미지 경계 통합 검증을 함께 "
            "통과한 뒤 운영 정책으로 동결했습니다. 측정값에 일정 배수를 "
            "기계적으로 적용해 산출한 값이 아닙니다."
            if final_limits_frozen
            else (
                "대표 라우터와 공식 출력·이미지 경계를 모두 검증하기 전까지 "
                "사용하는 잠정 상한입니다. 측정값에 일정 배수를 적용해 "
                "산출한 값이 아닙니다."
            )
        ),
        "limitations": [
            (
                "이번 Apple Silicon·Colima 측정은 공개 Train/Dev 입력만 사용합니다. "
                "최종 한도 동결에는 별도로 통과한 공식 출력·이미지 경계 통합 "
                "검증도 함께 사용했습니다."
                if final_limits_frozen
                else (
                    "이번 Apple Silicon·Colima 측정은 공개 Train/Dev 입력만 사용하며, "
                    "공식 출력 tmpfs·임시 공간과 OCI 크기 경계를 별도로 "
                    "검증하기 전에는 최종 자원 한도를 단독으로 확정하는 "
                    "근거가 아닙니다."
                )
                if apple_silicon_colima_completed
                else (
                    "현재 기록된 baseline 실행 성능은 최종 평가 장비가 "
                    "아닌 로컬 환경의 공개 Train/Dev 입력 측정입니다."
                )
            ),
            container_limitation,
            (
                "OCI 압축 계층 합계, Docker inspect Size와 실행 루트 파일 "
                "시스템은 서로 다른 지표이며 바꿔 쓸 수 없습니다."
            ),
            (
                "cgroup v2의 memory.peak, pids.peak와 CPU 사용·throttling "
                "값은 반복마다 측정했습니다. 출력 tmpfs·inode와 이미지 "
                "경계는 공식 런타임 통합 검증으로 확인했습니다."
                if final_limits_frozen
                else (
                    "cgroup v2의 memory.peak, pids.peak와 CPU 사용·throttling "
                    "값은 측정했습니다. 공식 출력 tmpfs의 사용량·inode 피크, "
                    "임시 저장공간 피크와 OCI 압축 계층 합계는 측정하지 "
                    "않았습니다."
                )
                if apple_silicon_colima_completed
                else (
                    "프로세스와 운영체제 스레드 피크, cgroup memory.peak, "
                    "임시 저장공간 피크와 선택한 플랫폼의 OCI 압축 계층 "
                    "합계는 이번 baseline 보고서에서 측정하지 않았습니다."
                )
            ),
            (
                "Apple Silicon 호스트, Colima와 linux/arm64 대표 측정 및 공식 "
                "출력·이미지 경계 검증을 마쳐 이 보고서의 자원 한도를 "
                "동결했습니다."
                if final_limits_frozen
                else (
                    "Apple Silicon 호스트, Colima와 linux/arm64 실행은 "
                    "확인했지만 공식 출력·이미지 경계 검증 전에는 최종 자원 "
                    "한도를 확정하지 않습니다."
                )
                if apple_silicon_colima_completed
                else (
                    "최종 평가에 사용할 Apple Silicon·Colima 환경에서 "
                    "검증하기 전에는 최종 자원 한도가 아닙니다."
                )
            ),
        ],
    }


def _summary_stats(repetitions: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    elapsed = [item["elapsed_seconds"] for item in repetitions]
    cpu = [item["cpu_seconds"] for item in repetitions]
    summary = {
        "elapsed_seconds_min": min(elapsed),
        "elapsed_seconds_median": statistics.median(elapsed),
        "elapsed_seconds_max": max(elapsed),
        "cpu_seconds_min": min(cpu),
        "cpu_seconds_median": statistics.median(cpu),
        "cpu_seconds_max": max(cpu),
        "max_rss_bytes": max(item["max_rss_bytes"] for item in repetitions),
        "max_processes": max(item["processes"] for item in repetitions),
        "max_threads": max(item["threads"] for item in repetitions),
        "output_bytes": max(item["output_bytes"] for item in repetitions),
    }
    cgroups = [
        item["cgroup_v2"]
        for item in repetitions
        if isinstance(item.get("cgroup_v2"), Mapping)
    ]
    if cgroups:
        summary["cgroup_v2_observed_max"] = {
            "cpu_usage_delta_usec": max(
                item["cpu_usage_delta_usec"] for item in cgroups
            ),
            "cpu_usage_usec": max(
                item["cpu_stat"]["usage_usec"] for item in cgroups
            ),
            "cpu_nr_throttled": max(
                item["cpu_stat"]["nr_throttled"] for item in cgroups
            ),
            "cpu_throttled_usec": max(
                item["cpu_stat"]["throttled_usec"] for item in cgroups
            ),
            "memory_peak_bytes": max(
                item["memory_peak_bytes"] for item in cgroups
            ),
            "pids_peak": max(item["pids_peak"] for item in cgroups),
        }
    else:
        summary["cgroup_v2_observed_max"] = None
    return summary


def _require_official_result_validity(
    results: Sequence[Mapping[str, Any]],
) -> None:
    invalid = [
        f"{result['strategy']}/{result['tier']}"
        for result in results
        if not result.get("deterministic")
        or not result.get("id_and_order_invariant")
    ]
    if invalid:
        raise RuntimeError(
            "공식 측정의 결정성 또는 ID·순서 감사 실패: "
            + ", ".join(invalid)
        )


def _human_bytes(value: Optional[int]) -> str:
    if value is None:
        return "측정하지 못함"
    return f"{value / (1024 * 1024):.2f} MiB"


def _human_limit_bytes(value: Optional[int]) -> str:
    if value is not None and value >= 1024**3 and value % 1024**3 == 0:
        return f"{value / 1024**3:.2f} GiB"
    return _human_bytes(value)


def _append_result_table(
    lines: List[str],
    results: Sequence[Mapping[str, Any]],
) -> None:
    lines.extend(
        [
            "| 구현 | 등급 | 경과 시간 중앙값/최댓값(초) | CPU 중앙값/최댓값(초) | 최대 RSS | 출력 크기 | 결정적 결과 | ID·순서 감사 |",
            "| --- | --- | ---: | ---: | ---: | ---: | --- | --- |",
        ]
    )
    for result in results:
        summary = result["summary"]
        lines.append(
            "| {strategy} | {tier} | {elapsed_median:.4f} / {elapsed_max:.4f} | "
            "{cpu_median:.4f} / {cpu_max:.4f} | {rss} | {output} | {deterministic} | "
            "{audit} |".format(
                strategy=result["strategy"],
                tier=result["tier"],
                elapsed_median=summary["elapsed_seconds_median"],
                elapsed_max=summary["elapsed_seconds_max"],
                cpu_median=summary["cpu_seconds_median"],
                cpu_max=summary["cpu_seconds_max"],
                rss=_human_bytes(summary["max_rss_bytes"]),
                output=_human_bytes(summary["output_bytes"]),
                deterministic="예" if result["deterministic"] else "아니요",
                audit="통과" if result["id_and_order_invariant"] else "실패",
            )
        )


def _markdown(report: Mapping[str, Any]) -> str:
    official = report.get("measurement_mode") == "apple-silicon-colima"
    proposal = report["resource_limit_proposal"]
    final_frozen = proposal.get("status") == "final-frozen"
    lines = [
        "<!--",
        "SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.",
        "SPDX-License-" + "Identifier: Apache-2.0",
        "-->",
        "",
        "# Baseline 실행 성능 측정",
        "",
        (
            "이 문서는 공개 Train/Dev 입력으로 수행한 Apple Silicon·Colima "
            "공식 플랫폼 참고 측정입니다."
            if official
            else "이 문서는 공개 Train/Dev 입력으로 수행한 로컬 예비 측정입니다."
        ),
        "공개 모델별 outcome, 정답, 모델 출력과 최종 평가 자료는 사용하지 않았습니다.",
        "",
        f"- 문항 수: {report['input']['episodes']:,}",
        f"- 반복 횟수: 구현·등급 조합마다 {report['repetitions']}",
        (
            "- 환경: "
            f"{report['environment']['system']} {report['environment']['release']} "
            f"{report['environment']['machine']}, Python {report['environment']['python']}"
        ),
        f"- 실행 입력 크기: {_human_bytes(report['input']['bytes'])}",
        (
            "- 반복 사이 운영체제 파일 페이지 캐시 강제 비우기: "
            + (
                "예"
                if report["measurement_methodology"][
                    "page_cache_dropped_between_repetitions"
                ]
                else "아니요"
            )
        ),
        "",
        "## 호스트 측정 결과",
        "",
    ]
    _append_result_table(lines, report["results"])
    if report["container_results"]:
        lines.extend(
            [
                "",
                "## 격리 컨테이너 측정 결과",
                "",
                "컨테이너 시작 시간을 포함한 경과 시간입니다. 모든 구현을 같은 이미지에서",
                "비교하기 위해 측정 중에만 진입점을 바꾸었으며, 공개 `router-run`",
                "인터페이스 검증은 별도의 통합 테스트에서 수행했습니다.",
                "",
            ]
        )
        _append_result_table(lines, report["container_results"])
        measurement_profile = report.get("measurement_profile", {}).get(
            "container"
        )
        if measurement_profile:
            memory_swap = measurement_profile.get("memory_swap_total_bytes")
            swap_text = (
                f", `--memory-swap` 합계 {_human_limit_bytes(memory_swap)}"
                if memory_swap is not None
                else ", `--memory-swap` 인자 미지정"
            )
            lines.extend(
                [
                    "",
                    (
                        "위 컨테이너 결과는 "
                        f"CPU {measurement_profile['cpu_cores']}코어, 메모리 "
                        f"{_human_limit_bytes(measurement_profile['memory_bytes'])}"
                        f"{swap_text}, PID·스레드 합계 "
                        f"{measurement_profile['pids_limit']}개로 측정했습니다."
                    ),
                    (
                        "임시 공간은 "
                        f"{_human_limit_bytes(measurement_profile['temporary_storage_bytes'])}"
                        "였으며, 이 값들은 아래 "
                        + ("최종 프로필" if final_frozen else "잠정 프로필")
                        + "을 적용한 "
                        + (
                            "공식 플랫폼 참고 측정 조건입니다."
                            if official
                            else "로컬 선별 측정 조건입니다."
                        )
                    ),
                ]
            )
        if official:
            cgroup_max = report["resource_limit_proposal"][
                "reference_router_observed_max"
            ].get("cgroup_v2_observed_max")
            if cgroup_max:
                lines.extend(
                    [
                        "",
                        "공식 플랫폼 cgroup v2 반복 측정의 관측 최댓값:",
                        "",
                        (
                            "- `memory.peak`: "
                            f"{_human_bytes(cgroup_max['memory_peak_bytes'])}"
                        ),
                        f"- `pids.peak`: {cgroup_max['pids_peak']}",
                        (
                            "- CPU 사용량 증가분: "
                            f"{cgroup_max['cpu_usage_delta_usec']} µs"
                        ),
                        (
                            "- `cpu.stat` 최종 사용량/제한 횟수/제한 시간: "
                            f"{cgroup_max['cpu_usage_usec']} µs / "
                            f"{cgroup_max['cpu_nr_throttled']} / "
                            f"{cgroup_max['cpu_throttled_usec']} µs"
                        ),
                    ]
                )
    image_limit = proposal["oci_compressed_image_limit_bytes"]
    rootfs_limit = proposal["rootfs_apparent_limit_bytes"]
    rootfs_measurement_label = (
        "겉보기 크기 하한"
        if report["container"].get("runtime_rootfs_apparent_is_lower_bound")
        else "겉보기 크기"
    )
    rootfs_warning = (
        [
            "루트 파일 시스템 측정은 일부 경로를 읽지 못했으므로 하한으로만",
            "기록합니다.",
        ]
        if report["container"].get("runtime_rootfs_apparent_is_lower_bound")
        else []
    )
    if report["container_results"]:
        server = report["container"].get("runtime_server") or {}
        container_status = [
            (
                f"Docker 컨텍스트 `{report['container']['runtime_context']}`에서 "
                "네트워크 없음, 읽기 전용 루트, 비특권 사용자, CPU·메모리·"
                "프로세스 제한을 적용해 실제 실행했습니다."
            ),
            (
                "컨테이너 서버 환경은 "
                f"{server.get('Os', 'unknown')}/{server.get('Arch', 'unknown')}입니다."
            ),
        ]
    elif report["container"]["runtime_available"]:
        container_status = [
            (
                "Docker 실행기를 확인해 지정한 로컬 이미지의 크기를 기록했지만 "
                "이미지가 지정되지 않아 격리 성능 측정은 실행하지 않았습니다."
            )
        ]
    else:
        container_status = [
            "이 환경에서 Docker 실행기에 연결하지 못해 이미지 빌드 크기, 네트워크 차단,",
            "읽기 전용 루트 파일 시스템의 실제 실행 오버헤드는 측정하지 못했습니다.",
            "Docker 통합 테스트는 구현했으며 실행기가 있는 환경에서 별도로 수행해야 합니다.",
        ]
    container_measurements = []
    if report["container"].get("image_size_bytes") is not None:
        container_measurements.extend(
            [
                "",
                (
                    "- `docker image inspect`에서 얻은 이미지 `Size`: "
                    f"`{report['container']['image_reference']}` "
                    f"({_human_bytes(report['container']['image_size_bytes'])})"
                ),
            ]
        )
    if report["container"].get("runtime_rootfs_apparent_bytes") is not None:
        container_measurements.append(
            "- 컨테이너 안에서 측정한 읽기 전용 루트 파일 시스템 "
            f"{rootfs_measurement_label}: "
            f"{_human_bytes(report['container']['runtime_rootfs_apparent_bytes'])}"
        )
    if report["container"].get("image_list_size_display") is not None:
        container_measurements.append(
            "- 로컬 데몬의 `docker image ls` 표시: "
            f"{report['container']['image_list_size_display']}"
        )
    if container_measurements:
        container_measurements.extend(
            [
                "",
                (
                    "위 로컬 값, OCI 매니페스트의 압축 계층 합계와 풀린 루트 "
                    "파일 시스템은"
                ),
                "서로 다른 지표이므로 바꿔 쓸 수 없습니다.",
                *rootfs_warning,
            ]
        )
    lines.extend(
        [
            "",
            "## " + ("최종 자원 한도" if final_frozen else "잠정 자원 한도"),
            "",
            (
                "아래 값은 대표 라우터 측정과 공식 출력·이미지 경계 검증을 "
                "통과해 동결한 등급별 컨테이너 한 번의 최종 상한입니다."
                if final_frozen
                else (
                    "아래 값은 등급별 컨테이너 한 번의 잠정 상한입니다. "
                    "대표 라우터와 공식 출력·이미지 경계를 같은 장비에서 "
                    "함께 검증한 뒤 동결합니다."
                )
            ),
            (
                f"현재 baseline 측정일은 {report['generated_at'][:10]}입니다."
            ),
            "",
            f"- CPU: {proposal['cpu_cores']}코어",
            f"- 전체 실행 시간: {proposal['wall_time_seconds']}초",
            (
                f"- 메모리: {_human_limit_bytes(proposal['memory_bytes'])}, "
                "추가 스왑 없음"
            ),
            (
                "- 프로세스·스레드: 합계 "
                f"{proposal['processes_and_threads_total']}개"
            ),
            (
                "- 제한 출력 볼륨: "
                f"{_human_bytes(proposal['output_bytes'])}, inode "
                f"{proposal['output_inodes']}개"
            ),
            (
                "- 실행 로그: 스트림별 "
                f"{_human_bytes(proposal['retained_log_bytes_per_stream'])} "
                "보관, "
                f"{_human_bytes(proposal['emitted_log_bytes_per_stream'])} "
                "출력량 한도"
            ),
            f"- 임시 디렉터리: {_human_bytes(proposal['temporary_storage_bytes'])}",
            (
                "- 선택한 플랫폼의 OCI 매니페스트(manifest)에 기록된 압축 "
                "계층(layer) 크기 합계 "
                + ("최종 한도: " if final_frozen else "잠정 한도: ")
                + f"{_human_limit_bytes(image_limit)}"
            ),
            (
                "- 풀린 실행 루트 파일 시스템(rootfs)의 겉보기 크기"
                "(apparent size) "
                + ("최종 한도: " if final_frozen else "잠정 한도: ")
                + f"{_human_limit_bytes(rootfs_limit)}"
            ),
            "",
            (
                (
                    "Apple Silicon 호스트·Colima·linux/arm64 대표 측정과 공식 "
                    "출력·이미지 경계 통합 검증을 마쳐 위 한도를 동결했습니다."
                    if final_frozen
                    else (
                        "Apple Silicon 호스트·Colima·linux/arm64 조건은 "
                        "확인했지만, 공식 출력·이미지 경계 검증 전에는 최종 "
                        "자원 한도를 확정하지 않습니다."
                    )
                )
                if official
                else (
                    "`linux/arm64`가 공식 컨테이너 플랫폼이지만 최종 장비 "
                    "측정은 아직 완료되지 않았습니다."
                )
            ),
            *(
                []
                if official
                else [
                    "최종 Apple Silicon·Colima 장비 측정은 완료되지 않았습니다.",
                ]
            ),
            "",
            "## 컨테이너 측정 상태",
            "",
            *container_status,
            *container_measurements,
            "",
            "고정한 기반 이미지는 `python:3.11.15-alpine3.23`이며 다중 플랫폼",
            f"인덱스 다이제스트는 `{report['container']['base_image_index_digest']}`입니다.",
            "세부 반복값과 해시는 [runtime-benchmark.json](runtime-benchmark.json)에",
            "기록했습니다.",
            "",
        ]
    )
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="대표 baseline의 실행 자원을 측정합니다."
    )
    parser.add_argument("--repetitions", type=int, default=DEFAULT_REPETITIONS)
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
    parser.add_argument(
        "--print-source-manifest-sha256",
        action="store_true",
        help=(
            "현재 공개 소스 파일 목록 SHA-256만 출력합니다. 컨테이너 빌드의 "
            "SOURCE_MANIFEST_SHA256 인자에 사용합니다."
        ),
    )
    parser.add_argument(
        "--measurement-mode",
        choices=MEASUREMENT_MODES,
        default="local-screening",
        help=(
            "local-screening은 예비 측정이고 apple-silicon-colima는 "
            "Apple Silicon·Colima·linux/arm64 환경에서 fail-closed로 "
            "실행합니다."
        ),
    )
    parser.add_argument(
        "--apple-silicon-operator-attestation",
        help=(
            "apple-silicon-colima에서 운영자가 "
            f"{APPLE_SILICON_OPERATOR_ATTESTATION} 값을 명시해야 합니다."
        ),
    )
    parser.add_argument(
        "--apple-silicon-environment-label",
        help=(
            "측정 장비와 Colima 구성을 식별하는 운영자 지정 "
            "1~128자 레이블입니다."
        ),
    )
    parser.add_argument(
        "--final-runtime-boundary-attestation",
        help=(
            "공식 출력 tmpfs·inode·격리·이미지 경계 통합 테스트를 통과한 "
            "뒤에만 "
            f"{FINAL_RUNTIME_BOUNDARY_ATTESTATION} 값을 명시합니다."
        ),
    )
    parser.add_argument(
        "--colima-profile",
        default=DEFAULT_COLIMA_PROFILE,
        help="공식 측정에 사용할 Colima profile 이름입니다.",
    )
    parser.add_argument(
        "--json-output",
        type=pathlib.Path,
        default=ROOT / "docs/runtime-benchmark.json",
    )
    parser.add_argument(
        "--markdown-output",
        type=pathlib.Path,
        default=ROOT / "docs/runtime-benchmark.md",
    )
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--container-image",
        help="Docker가 있으면 이 로컬 이미지로 격리 성능도 측정합니다.",
    )
    parser.add_argument(
        "--strategy",
        choices=STRATEGIES,
        default="prompt-heuristic",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--tier", choices=TIERS, default="fast", help=argparse.SUPPRESS)
    parser.add_argument("--input", type=pathlib.Path, help=argparse.SUPPRESS)
    parser.add_argument("--output", type=pathlib.Path, help=argparse.SUPPRESS)
    return parser


def _stage_report_text(destination: pathlib.Path, content: str) -> pathlib.Path:
    descriptor, name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".staged",
        dir=destination.parent,
    )
    staged = pathlib.Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fchmod(stream.fileno(), 0o644)
            os.fsync(stream.fileno())
    except BaseException:
        try:
            staged.unlink()
        except FileNotFoundError:
            pass
        raise
    return staged


def _fsync_directory(path: pathlib.Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _report_transaction_marker_path(
    json_output: pathlib.Path,
    markdown_output: pathlib.Path,
) -> pathlib.Path:
    identity = "\0".join(
        (
            str(json_output.resolve()),
            str(markdown_output.resolve()),
        )
    ).encode("utf-8")
    suffix = hashlib.sha256(identity).hexdigest()[:16]
    return json_output.parent / f".ossp-report-pair-{suffix}.transaction.json"


def _create_report_transaction_marker(
    marker: pathlib.Path,
    *,
    json_output: pathlib.Path,
    markdown_output: pathlib.Path,
) -> None:
    payload = (
        json.dumps(
            {
                "schema_version": 1,
                "status": "publish-in-progress",
                "json_output": str(json_output.resolve()),
                "markdown_output": str(markdown_output.resolve()),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    try:
        with marker.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fchmod(stream.fileno(), 0o600)
            os.fsync(stream.fileno())
    except FileExistsError as exc:
        raise RuntimeError(
            "이전 보고서 쌍 게시의 durable transaction marker가 남아 "
            "있습니다. JSON/Markdown 쌍을 검토하고 marker를 명시적으로 "
            "복구하기 전에는 새 보고서를 게시하지 않습니다."
        ) from exc
    _fsync_directory(marker.parent)


def _publish_report_pair(
    *,
    json_output: pathlib.Path,
    markdown_output: pathlib.Path,
    report: Mapping[str, Any],
) -> None:
    if json_output.resolve() == markdown_output.resolve():
        raise ValueError("JSON과 Markdown 출력 경로는 달라야 합니다.")
    json_output.parent.mkdir(parents=True, exist_ok=True)
    markdown_output.parent.mkdir(parents=True, exist_ok=True)
    contents = (
        (
            json_output,
            json.dumps(
                report,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
        ),
        (markdown_output, _markdown(report)),
    )
    marker = _report_transaction_marker_path(json_output, markdown_output)
    _create_report_transaction_marker(
        marker,
        json_output=json_output,
        markdown_output=markdown_output,
    )
    staged: Dict[pathlib.Path, pathlib.Path] = {}
    backups: Dict[pathlib.Path, pathlib.Path] = {}
    published: List[pathlib.Path] = []
    try:
        for destination, content in contents:
            staged[destination] = _stage_report_text(destination, content)
        for destination, _content in contents:
            if destination.exists():
                descriptor, backup_name = tempfile.mkstemp(
                    prefix=f".{destination.name}.",
                    suffix=".backup",
                    dir=destination.parent,
                )
                os.close(descriptor)
                backup = pathlib.Path(backup_name)
                backup.unlink()
                os.replace(destination, backup)
                backups[destination] = backup
        for destination, _content in contents:
            os.replace(staged[destination], destination)
            published.append(destination)
        for parent in {destination.parent for destination, _content in contents}:
            _fsync_directory(parent)
        marker.unlink()
        _fsync_directory(marker.parent)
    except BaseException:
        for destination in published:
            try:
                destination.unlink()
            except FileNotFoundError:
                pass
        for destination, backup in backups.items():
            if backup.exists():
                os.replace(backup, destination)
        for parent in {destination.parent for destination, _content in contents}:
            _fsync_directory(parent)
        try:
            marker.unlink()
        except FileNotFoundError:
            pass
        else:
            _fsync_directory(marker.parent)
        raise
    finally:
        for path in [*staged.values(), *backups.values()]:
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    if args.print_source_manifest_sha256:
        print(_source_tree_manifest()["sha256"])
        return 0
    if args.worker:
        if args.input is None or args.output is None:
            raise SystemExit("--worker에는 --input과 --output이 필요합니다.")
        return _worker(args)
    if args.repetitions < 5:
        raise SystemExit("--repetitions는 5 이상이어야 합니다.")

    official_mode = args.measurement_mode == "apple-silicon-colima"
    final_boundary_completed = (
        args.final_runtime_boundary_attestation
        == FINAL_RUNTIME_BOUNDARY_ATTESTATION
    )
    if args.final_runtime_boundary_attestation and not final_boundary_completed:
        raise SystemExit("최종 런타임 경계 attestation 값이 올바르지 않습니다.")
    if final_boundary_completed and not official_mode:
        raise SystemExit(
            "최종 런타임 경계 attestation은 apple-silicon-colima 측정에서만 "
            "사용할 수 있습니다."
        )
    if official_mode and not args.container_image:
        raise SystemExit(
            "apple-silicon-colima 측정에는 --container-image가 필요합니다."
        )
    source_tree_manifest = _source_tree_manifest()
    docker = shutil.which("docker")
    colima = shutil.which("colima")
    try:
        official_environment = (
            _probe_apple_silicon_environment(
                docker=docker,
                colima=colima,
                colima_profile=args.colima_profile,
                attestation=args.apple_silicon_operator_attestation,
                environment_label=args.apple_silicon_environment_label,
            )
            if official_mode
            else None
        )
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc

    resolved_image: Optional[Dict[str, Any]] = None
    if args.container_image and docker is not None:
        daemon = subprocess.run(
            [docker, "info"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        if daemon.returncode == 0:
            try:
                resolved_image = _resolve_container_image(
                    docker,
                    args.container_image,
                )
            except RuntimeError as exc:
                raise SystemExit(str(exc)) from exc
    if resolved_image is not None:
        try:
            _require_image_source_binding(
                resolved_image,
                current_source_manifest_sha256=source_tree_manifest["sha256"],
            )
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc
    if args.container_image and resolved_image is None:
        raise SystemExit(
            "요청한 로컬 컨테이너 이미지를 확인하지 못해 측정을 "
            "계속할 수 없습니다."
        )
    if official_mode and resolved_image is not None:
        platform_value = resolved_image["platform"]
        if (
            platform_value["os"] != "linux"
            or platform_value["architecture"] not in {"arm64", "aarch64"}
        ):
            raise SystemExit("공식 측정용 이미지가 linux/arm64가 아닙니다.")

    container = _container_measurement(resolved_image)
    run_container_benchmark = bool(
        container["runtime_available"] and resolved_image is not None
    )
    if official_mode and not run_container_benchmark:
        raise SystemExit("공식 컨테이너 측정 환경을 사용할 수 없습니다.")
    temporary_parent = pathlib.Path.home() if run_container_benchmark else None
    with tempfile.TemporaryDirectory(
        prefix=".ossp-router-benchmark-",
        dir=temporary_parent,
    ) as temporary:
        target = pathlib.Path(temporary)
        target.chmod(0o755)
        input_path = target / "inputs.json"
        variant_path = target / "inputs-audit.json"
        try:
            public_inputs = load_public_runtime_workload(
                train_path=args.train_input,
                dev_path=args.dev_input,
                registry_path=args.registry,
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        write_json(input_path, input_batch_to_dict(public_inputs))
        inputs = load_input(input_path)
        audit_variant = make_id_order_audit_variant(
            inputs,
            secret=b"public-train-dev-runtime-benchmark-audit-v1",
        )
        write_json(variant_path, input_batch_to_dict(audit_variant.inputs))
        variant_inputs = load_input(variant_path)
        input_path.chmod(0o644)
        variant_path.chmod(0o644)

        results: List[Dict[str, Any]] = []
        for strategy in STRATEGIES:
            for tier in TIERS:
                repetitions = []
                for repetition in range(args.repetitions):
                    output = target / f"{strategy}-{tier}-{repetition}.json"
                    repetitions.append(
                        _run_once(
                            strategy=strategy,
                            tier=tier,
                            input_path=input_path,
                            output_path=output,
                            inputs=inputs,
                        )
                    )
                hashes = {item["output_sha256"] for item in repetitions}
                result = {
                    "strategy": strategy,
                    "tier": tier,
                    "repetitions": repetitions,
                    "summary": _summary_stats(repetitions),
                    "deterministic": len(hashes) == 1,
                    "id_and_order_invariant": _audit_invariance(
                        strategy=strategy,
                        tier=tier,
                        original_path=input_path,
                        original_inputs=inputs,
                        variant_path=variant_path,
                        variant_inputs=variant_inputs,
                        target=target,
                        id_mapping=audit_variant.original_to_audit_id,
                    ),
                }
                results.append(result)

        container_results: List[Dict[str, Any]] = []
        if run_container_benchmark:
            if docker is None:
                raise RuntimeError("Docker CLI 경로가 측정 중 사라졌습니다.")
            image = str(resolved_image["resolved_local_content_id"])
            container_output = target / "container-output"
            container_output.mkdir()
            container_output.chmod(0o777)
            for strategy in STRATEGIES:
                for tier in TIERS:
                    repetitions = []
                    for repetition in range(args.repetitions):
                        output = (
                            container_output
                            / f"{strategy}-{tier}-{repetition}.json"
                        )
                        repetitions.append(
                            _run_container_once(
                                docker=docker,
                                image=image,
                                strategy=strategy,
                                tier=tier,
                                input_path=input_path,
                                output_path=output,
                                inputs=inputs,
                                require_official_cgroup=official_mode,
                            )
                        )
                    hashes = {
                        item["output_sha256"] for item in repetitions
                    }
                    result = {
                        "strategy": strategy,
                        "tier": tier,
                        "repetitions": repetitions,
                        "summary": _summary_stats(repetitions),
                        "deterministic": len(hashes) == 1,
                        "id_and_order_invariant": (
                            _audit_container_invariance(
                                docker=docker,
                                image=image,
                                strategy=strategy,
                                tier=tier,
                                original_path=input_path,
                                original_inputs=inputs,
                                variant_path=variant_path,
                                variant_inputs=variant_inputs,
                                target=container_output,
                                id_mapping=(
                                    audit_variant.original_to_audit_id
                                ),
                                require_official_cgroup=official_mode,
                            )
                        ),
                    }
                    container_results.append(result)
        if official_mode:
            _require_official_result_validity(
                [*results, *container_results]
            )
        final_source_tree_manifest = _source_tree_manifest()
        if final_source_tree_manifest["sha256"] != source_tree_manifest["sha256"]:
            raise RuntimeError(
                "측정 중 공개 소스 트리가 변경되어 보고서를 게시하지 않습니다."
            )
        report = {
            "schema_version": 3,
            "report_type": "reference-router-runtime-benchmark",
            "measurement_mode": args.measurement_mode,
            "measurement_status": (
                "apple-silicon-colima-reference-completed"
                if official_mode
                else "local-screening-completed"
            ),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "input": {
                "kind": "materialized-public-train-dev-inputs",
                "profile_id": PUBLIC_RUNTIME_PROFILE_ID,
                "basis": "materialized-public-train-dev-inputs",
                "represents_evaluation_distribution": False,
                "episodes": len(inputs.episodes),
                "bytes": input_path.stat().st_size,
                "sha256": hashlib.sha256(input_path.read_bytes()).hexdigest(),
            },
            "repetitions": args.repetitions,
            "environment": _environment(),
            "apple_silicon_environment_evidence": official_environment,
            "measurement_methodology": {
                "fresh_process_per_repetition": True,
                "fresh_container_per_container_repetition": (
                    run_container_benchmark
                ),
                "page_cache_dropped_between_repetitions": False,
                "page_cache_policy": (
                    "운영체제 파일 페이지 캐시는 반복 사이에 강제로 "
                    "비우지 않았습니다."
                ),
                "final_runtime_boundary_validation": (
                    {
                        "operator_attestation": (
                            FINAL_RUNTIME_BOUNDARY_ATTESTATION
                        ),
                        "test_command": (
                            "OSSP_RUN_CONTAINER_TESTS=1 PYTHONPATH=src "
                            "python3 -m unittest tests.test_runtime"
                        ),
                        "observed_result": "Ran 83 tests; OK",
                        "covered_boundaries": [
                            "official tmpfs output bytes and inode limits",
                            "extra output and declared VOLUME rejection",
                            "read-only root and non-privileged execution",
                            "network and device isolation",
                            "rootfs measurement and immutable image resolution",
                        ],
                    }
                    if final_boundary_completed
                    else None
                ),
            },
            "source_tree_manifest": final_source_tree_manifest,
            "artifact_hashes": _artifact_hashes(),
            "measurement_profile": _measurement_profile(run_container_benchmark),
            "execution_fidelity": {
                "output_storage": "host-bind-directory",
                "final_evaluation_equivalent": False,
                "limitation": (
                    "이 도구의 출력은 측정 결과 회수를 위한 host bind입니다. "
                    "공식 4 MiB tmpfs 출력 볼륨과 동일한 경계로 간주하지 않습니다."
                ),
                "required_final_output_boundary": {
                    "storage": "tmpfs-volume",
                    "bytes": PHASE_C_CANDIDATE_LIMITS.output_bytes,
                    "inodes": PHASE_C_CANDIDATE_LIMITS.output_inodes,
                    "implemented_by_this_benchmark": False,
                },
                "container_image_resolution": (
                    "요청 참조를 한 번 local content ID로 고정하고 "
                    "모든 실행에 --pull never를 사용합니다."
                    if resolved_image is not None
                    else None
                ),
                "container_isolation": {
                    "network": "none",
                    "cgroup_namespace": "private",
                    "ipc": "none",
                    "read_only_root": True,
                    "user": "65532:65532",
                    "cap_drop": "ALL",
                    "no_new_privileges": True,
                    "core_ulimit": 0,
                    "log_driver": "none",
                },
            },
            "results": results,
            "container_results": container_results,
            "container": container,
            "resolved_image": resolved_image,
            "resource_limit_proposal": _proposal(
                results,
                container_results,
                container,
                apple_silicon_colima_completed=official_mode,
                final_runtime_boundary_validation_completed=(
                    final_boundary_completed
                ),
            ),
        }
    _publish_report_pair(
        json_output=args.json_output,
        markdown_output=args.markdown_output,
        report=report,
    )
    print(f"OK: {args.json_output} 및 {args.markdown_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
