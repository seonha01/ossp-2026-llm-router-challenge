# SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
# SPDX-License-Identifier: Apache-2.0

"""Generate bounded operator evidence for the official OCI image platform."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import signal
import stat
import subprocess
import tarfile
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple

from .protocol import dumps_json
from .runtime import (
    ATTEMPT_LABEL,
    DeclaredVolumeNotAllowed,
    IMAGE_SIZE_EVIDENCE_MAX_BYTES,
    OCI_LAYER_MEASUREMENT_METHOD,
    OFFICIAL_CONTAINER_ARCHITECTURE,
    OFFICIAL_CONTAINER_OS,
    OFFICIAL_CONTAINER_PLATFORM,
    PHASE_C_CANDIDATE_LIMITS,
    ROOTFS_MEASUREMENT_METHOD,
    ImageLimitExceeded,
    ImagePreflightRejected,
    ImageSizeEvidence,
    InfrastructureUnavailable,
    _control_failure_detail,
    _control_target_absent,
    _run_control_command,
    inspect_image_runtime_metadata,
    inspect_runtime_daemon_id,
    is_immutable_image_reference,
    is_named_image_digest,
    validate_image_configuration,
    validate_image_size_evidence,
    validate_operator_directory,
)


_DIGEST = re.compile(r"^sha256:([0-9a-f]{64})$")
_OCI_INDEX_MEDIA_TYPES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}
_OCI_MANIFEST_MEDIA_TYPES = {
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
}
_MAX_OCI_METADATA_BYTES = 4 * 1024 * 1024
_MAX_CONFIG_BYTES = 16 * 1024 * 1024
_MAX_EXPORT_STREAM_BYTES = (
    PHASE_C_CANDIDATE_LIMITS.rootfs_apparent_bytes + 512 * 1024 * 1024
)
_MAX_EXPORT_MEMBERS = 2_000_000
_MAX_EXPORT_SECONDS = 600.0
_MAX_EXPORT_STDERR_BYTES = 64 * 1024
_MEASUREMENT_JOURNAL_NAME = (
    ".ossp-router-image-measurement-resource-journal.v1.json"
)


def _read_regular_file(path: Path, maximum: int) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(str(path), flags)
    except OSError as exc:
        raise InfrastructureUnavailable(
            f"OCI layout 파일을 안전하게 열 수 없습니다: {path.name}: {exc}"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size > maximum
        ):
            raise InfrastructureUnavailable(
                f"OCI layout 파일이 단일 일반 파일이 아니거나 "
                f"{maximum}바이트를 넘습니다: {path.name}"
            )
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            return stream.read(maximum + 1)
    finally:
        os.close(descriptor)


def _decode_json_object(payload: bytes, label: str) -> Mapping[str, Any]:
    def reject_duplicates(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"중복 JSON 키: {key}")
            value[key] = item
        return value

    try:
        value = json.loads(payload, object_pairs_hook=reject_duplicates)
    except (RecursionError, UnicodeError, ValueError) as exc:
        raise InfrastructureUnavailable(f"{label} JSON을 해석할 수 없습니다.") from exc
    if not isinstance(value, Mapping):
        raise InfrastructureUnavailable(f"{label} JSON 최상위 값은 객체여야 합니다.")
    return value


def _load_json_blob(path: Path, maximum: int) -> Tuple[bytes, Mapping[str, Any]]:
    payload = _read_regular_file(path, maximum)
    return payload, _decode_json_object(payload, path.name)


def _blob_path(layout: Path, digest: str) -> Path:
    match = _DIGEST.fullmatch(digest)
    if match is None:
        raise InfrastructureUnavailable("OCI descriptor digest 형식이 올바르지 않습니다.")
    return layout / "blobs" / "sha256" / match.group(1)


def _checked_blob(
    layout: Path,
    descriptor: Mapping[str, Any],
    maximum: int,
) -> bytes:
    digest = descriptor.get("digest")
    size = descriptor.get("size")
    if (
        not isinstance(digest, str)
        or isinstance(size, bool)
        or not isinstance(size, int)
        or size < 0
        or size > maximum
    ):
        raise InfrastructureUnavailable(
            "OCI descriptor의 digest 또는 size가 올바르지 않습니다."
        )
    payload = _read_regular_file(_blob_path(layout, digest), maximum)
    if len(payload) != size or "sha256:" + hashlib.sha256(payload).hexdigest() != digest:
        raise InfrastructureUnavailable(
            "OCI blob의 실제 크기 또는 SHA-256이 descriptor와 다릅니다."
        )
    return payload


def _select_official_platform_manifest(
    layout: Path,
    submitted_digest: str,
) -> Tuple[str, Mapping[str, Any]]:
    digest = submitted_digest.rsplit("@", 1)[1]
    _layout_payload, layout_index = _load_json_blob(
        layout / "index.json",
        _MAX_OCI_METADATA_BYTES,
    )
    descriptors = layout_index.get("manifests")
    if not isinstance(descriptors, list):
        raise InfrastructureUnavailable("OCI layout index의 manifests가 올바르지 않습니다.")
    roots = [
        item
        for item in descriptors
        if isinstance(item, Mapping) and item.get("digest") == digest
    ]
    if len(roots) != 1:
        raise InfrastructureUnavailable(
            "제출 repository digest를 OCI layout index에서 하나로 찾을 수 없습니다."
        )
    root = roots[0]
    root_payload = _checked_blob(layout, root, _MAX_OCI_METADATA_BYTES)
    root_value = _decode_json_object(root_payload, "제출 OCI descriptor")
    media_type = root.get("mediaType")
    if media_type in _OCI_MANIFEST_MEDIA_TYPES:
        return digest, root_value
    if media_type not in _OCI_INDEX_MEDIA_TYPES:
        raise InfrastructureUnavailable("지원하지 않는 OCI index media type입니다.")
    manifests = root_value.get("manifests")
    if not isinstance(manifests, list):
        raise InfrastructureUnavailable("OCI image index의 manifests가 올바르지 않습니다.")
    candidates = []
    for descriptor in manifests:
        if not isinstance(descriptor, Mapping):
            continue
        platform = descriptor.get("platform")
        if (
            isinstance(platform, Mapping)
            and platform.get("os") == OFFICIAL_CONTAINER_OS
            and platform.get("architecture") == OFFICIAL_CONTAINER_ARCHITECTURE
            and descriptor.get("mediaType") in _OCI_MANIFEST_MEDIA_TYPES
        ):
            candidates.append(descriptor)
    if len(candidates) != 1:
        raise InfrastructureUnavailable(
            f"{OFFICIAL_CONTAINER_PLATFORM} OCI manifest descriptor를 "
            "하나로 선택할 수 없습니다."
        )
    selected = candidates[0]
    payload = _checked_blob(layout, selected, _MAX_OCI_METADATA_BYTES)
    manifest = _decode_json_object(
        payload,
        f"{OFFICIAL_CONTAINER_PLATFORM} manifest",
    )
    return str(selected["digest"]), manifest


def measure_oci_layout(
    layout: Path,
    submitted_digest: str,
) -> Tuple[str, str, int]:
    """Verify one OCI layout and return manifest, config and compressed bytes."""

    if not is_named_image_digest(submitted_digest):
        raise ValueError("제출 이미지는 변경 불가능한 repository digest여야 합니다.")
    try:
        layout_metadata = layout.lstat()
    except OSError as exc:
        raise InfrastructureUnavailable(f"OCI layout을 검사할 수 없습니다: {exc}") from exc
    if not stat.S_ISDIR(layout_metadata.st_mode) or stat.S_ISLNK(
        layout_metadata.st_mode
    ):
        raise InfrastructureUnavailable("OCI layout 경로는 실제 디렉터리여야 합니다.")
    for directory in (layout / "blobs", layout / "blobs" / "sha256"):
        try:
            metadata = directory.lstat()
        except OSError as exc:
            raise InfrastructureUnavailable(
                f"OCI blob 경로를 검사할 수 없습니다: {exc}"
            ) from exc
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(
            metadata.st_mode
        ):
            raise InfrastructureUnavailable(
                "OCI blob 경로에는 심볼릭 링크를 사용할 수 없습니다."
            )
    _layout_bytes, layout_marker = _load_json_blob(
        layout / "oci-layout",
        1024,
    )
    if layout_marker != {"imageLayoutVersion": "1.0.0"}:
        raise InfrastructureUnavailable("지원하지 않는 OCI layout 버전입니다.")
    selected_digest, manifest = _select_official_platform_manifest(
        layout,
        submitted_digest,
    )
    config = manifest.get("config")
    layers = manifest.get("layers")
    if not isinstance(config, Mapping) or not isinstance(layers, list):
        raise InfrastructureUnavailable("OCI manifest의 config 또는 layers가 올바르지 않습니다.")
    config_payload = _checked_blob(layout, config, _MAX_CONFIG_BYTES)
    config_value = _decode_json_object(config_payload, "OCI image config")
    if (
        config_value.get("os") != OFFICIAL_CONTAINER_OS
        or config_value.get("architecture") != OFFICIAL_CONTAINER_ARCHITECTURE
    ):
        raise InfrastructureUnavailable(
            f"OCI image config 플랫폼이 {OFFICIAL_CONTAINER_PLATFORM}이 아닙니다."
        )
    runtime_config = config_value.get("config")
    if runtime_config is not None and not isinstance(runtime_config, Mapping):
        raise InfrastructureUnavailable("OCI image runtime config가 객체가 아닙니다.")
    if isinstance(runtime_config, Mapping):
        volumes = runtime_config.get("Volumes")
        if volumes is not None and not isinstance(volumes, Mapping):
            raise InfrastructureUnavailable(
                "OCI image config의 Volumes가 객체가 아닙니다."
            )
        if volumes:
            raise DeclaredVolumeNotAllowed(
                "이미지의 VOLUME 선언은 허용하지 않습니다."
            )
    total = 0
    for descriptor in layers:
        if not isinstance(descriptor, Mapping):
            raise InfrastructureUnavailable("OCI layer descriptor가 객체가 아닙니다.")
        size = descriptor.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise InfrastructureUnavailable("OCI layer size가 올바르지 않습니다.")
        total += size
        if total > PHASE_C_CANDIDATE_LIMITS.oci_compressed_image_bytes:
            raise ImageLimitExceeded(
                "OCI 압축 계층 합계가 공식 한도를 초과합니다."
            )
        _checked_blob(
            layout,
            descriptor,
            PHASE_C_CANDIDATE_LIMITS.oci_compressed_image_bytes,
        )
    config_digest = config.get("digest")
    if not isinstance(config_digest, str) or not is_immutable_image_reference(
        config_digest
    ):
        raise InfrastructureUnavailable("OCI config digest가 올바르지 않습니다.")
    return selected_digest, config_digest, total


class _BoundedExportReader:
    def __init__(self, stream: Any, process: subprocess.Popen[bytes]) -> None:
        self.stream = stream
        self.process = process
        self.total = 0
        self.deadline = time.monotonic() + _MAX_EXPORT_SECONDS

    def read(self, size: int = -1) -> bytes:
        if time.monotonic() > self.deadline:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            raise InfrastructureUnavailable("Docker export가 제한 시간을 넘었습니다.")
        chunk = self.stream.read(size)
        self.total += len(chunk)
        if self.total > _MAX_EXPORT_STREAM_BYTES:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            raise ImageLimitExceeded(
                "Docker export 스트림이 운영자 상한을 초과합니다."
            )
        return chunk


def _measure_export_tar(
    runtime_command: Sequence[str],
    image_reference: str,
    platform: str,
    resource_journal_path: Path,
) -> int:
    token = secrets.token_hex(16)
    name = f"ossp-router-image-evidence-{os.getpid()}-{token[:12]}"
    process: Optional[subprocess.Popen[bytes]] = None
    watchdog: Optional[threading.Timer] = None
    stderr_thread: Optional[threading.Thread] = None
    create_attempted = False
    create_confirmed = False
    create_uncertain = True
    stderr_data = bytearray()
    stderr_exceeded = threading.Event()

    def drain_stderr() -> None:
        assert process is not None and process.stderr is not None
        while True:
            chunk = process.stderr.read(64 * 1024)
            if not chunk:
                return
            remaining = _MAX_EXPORT_STDERR_BYTES + 1 - len(stderr_data)
            if remaining > 0:
                stderr_data.extend(chunk[:remaining])
            if len(stderr_data) > _MAX_EXPORT_STDERR_BYTES:
                stderr_exceeded.set()
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                return

    def kill_export() -> None:
        if process is None:
            return
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    try:
        daemon_id = inspect_runtime_daemon_id(runtime_command)
        _write_measurement_journal(
            resource_journal_path,
            {
                "schema_version": 1,
                "report_type": (
                    "operator-image-measurement-resource-journal"
                ),
                "container_name": name,
                "attempt_token": token,
                "create_confirmed": False,
                "create_uncertain": True,
                "runtime_command": list(runtime_command),
                "daemon_id": daemon_id,
            },
        )
        create_attempted = True
        created, create_error = _run_control_command(
            [
                *runtime_command,
                "create",
                "--platform",
                platform,
                "--name",
                name,
                "--label",
                f"{ATTEMPT_LABEL}=image-evidence-{token}",
                "--pull",
                "never",
                "--entrypoint",
                "/",
                image_reference,
            ],
            timeout_seconds=30,
            max_stdout_bytes=4096,
            max_stderr_bytes=64 * 1024,
        )
        if (
            create_error is not None
            or created is None
            or created.returncode != 0
        ):
            # A Docker CLI nonzero result does not prove that the daemon
            # rejected the mutation before accepting it. Preserve the intent
            # journal until a labeled resource is removed or the operator
            # confirms a daemon restart.
            create_uncertain = True
            raise InfrastructureUnavailable(
                _control_failure_detail(
                    "rootfs 측정 컨테이너를 만들 수 없음",
                    created,
                    create_error,
                )
            )
        create_confirmed = True
        create_uncertain = False
        _write_measurement_journal(
            resource_journal_path,
            {
                "schema_version": 1,
                "report_type": (
                    "operator-image-measurement-resource-journal"
                ),
                "container_name": name,
                "attempt_token": token,
                "create_confirmed": True,
                "create_uncertain": False,
                "runtime_command": list(runtime_command),
                "daemon_id": daemon_id,
            },
            replace_existing=True,
        )
        process = subprocess.Popen(
            [*runtime_command, "export", name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        assert process.stdout is not None
        watchdog = threading.Timer(
            _MAX_EXPORT_SECONDS,
            kill_export,
        )
        watchdog.daemon = True
        watchdog.start()
        stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
        stderr_thread.start()
        reader = _BoundedExportReader(process.stdout, process)
        apparent = 0
        members = 0
        with tarfile.open(fileobj=reader, mode="r|") as archive:
            for member in archive:
                members += 1
                if members > _MAX_EXPORT_MEMBERS:
                    raise ImageLimitExceeded(
                        "rootfs 항목 수가 운영자 상한을 초과합니다."
                    )
                if member.isfile():
                    apparent += member.size
                    if apparent > PHASE_C_CANDIDATE_LIMITS.rootfs_apparent_bytes:
                        raise ImageLimitExceeded(
                            "rootfs 겉보기 크기가 공식 한도를 초과합니다."
                        )
        returncode = process.wait(timeout=5)
        stderr_thread.join(timeout=1)
        if stderr_exceeded.is_set() or returncode != 0:
            raise InfrastructureUnavailable(
                "Docker export가 실패했거나 stderr 상한을 초과했습니다."
            )
        return apparent
    except (OSError, subprocess.TimeoutExpired, tarfile.TarError) as exc:
        raise InfrastructureUnavailable(f"Docker export를 검사할 수 없습니다: {exc}") from exc
    finally:
        if watchdog is not None:
            watchdog.cancel()
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        if stderr_thread is not None:
            stderr_thread.join(timeout=1)
        if process is not None:
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
        cleanup_error = (
            _cleanup_measurement_container(
                runtime_command,
                name,
                attempt_token=token,
                create_confirmed=create_confirmed,
                create_uncertain=create_uncertain,
            )
            if create_attempted
            else None
        )
        if cleanup_error is not None:
            raise InfrastructureUnavailable(cleanup_error)
        if create_attempted:
            _remove_measurement_journal(resource_journal_path)


def _cleanup_measurement_container(
    runtime_command: Sequence[str],
    name: str,
    *,
    attempt_token: str,
    create_confirmed: bool,
    create_uncertain: bool,
) -> Optional[str]:
    """Boundedly find and remove a container after even an uncertain create."""

    last_control_error = None
    for delay in (0.0, 0.1, 0.3, 0.6):
        if delay:
            time.sleep(delay)
        inspected, inspect_error = _run_control_command(
            [
                *runtime_command,
                "container",
                "inspect",
                "--format",
                "\n".join(
                    (
                        "{{json .Id}}",
                        f'{{{{json (index .Config.Labels "{ATTEMPT_LABEL}")}}}}',
                    )
                ),
                name,
            ],
            timeout_seconds=10,
            max_stdout_bytes=4096,
            max_stderr_bytes=4096,
        )
        if inspect_error is not None or inspected is None:
            last_control_error = _control_failure_detail(
                "rootfs 측정 컨테이너를 확인할 수 없음",
                inspected,
                inspect_error,
            )
            continue
        if inspected.returncode != 0:
            continue
        try:
            lines = inspected.stdout.decode("utf-8").splitlines()
            if len(lines) != 2:
                raise ValueError("inspect 줄 수")
            _container_id, observed_label = (
                json.loads(line) for line in lines
            )
        except (RecursionError, UnicodeError, ValueError):
            return "rootfs 측정 컨테이너 검사 응답이 올바르지 않습니다."
        if observed_label != f"image-evidence-{attempt_token}":
            return "rootfs 측정 컨테이너의 시도 라벨이 다릅니다."
        removed, remove_error = _run_control_command(
            [*runtime_command, "rm", "--force", name],
            timeout_seconds=30,
            max_stdout_bytes=4096,
            max_stderr_bytes=64 * 1024,
        )
        if (
            remove_error is not None
            or removed is None
            or removed.returncode != 0
        ):
            return _control_failure_detail(
                "rootfs 측정 컨테이너를 정리할 수 없음",
                removed,
                remove_error,
            )
        return None
    if create_confirmed:
        return (
            last_control_error
            or "생성된 rootfs 측정 컨테이너를 다시 확인할 수 없습니다."
        )
    if create_uncertain:
        return (
            last_control_error
            or "Docker create 결과가 불확실하여 이미지 측정 자원 저널을 "
            "보존했습니다. 운영자 확인과 정리가 필요합니다."
        )
    return last_control_error


def _write_measurement_journal(
    path: Path,
    value: Mapping[str, Any],
    *,
    replace_existing: bool = False,
) -> None:
    parent = validate_operator_directory(
        path.parent,
        "이미지 측정 저널 상위 경로",
    )
    path = parent / path.name
    payload = dumps_json(value).encode("utf-8")
    temporary = parent / f".{path.name}.operator-{secrets.token_hex(8)}"
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
        if replace_existing:
            os.replace(temporary, path)
        else:
            os.link(temporary, path, follow_symlinks=False)
            temporary.unlink()
        directory = os.open(str(parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        raise InfrastructureUnavailable(
            "보류 중인 이미지 측정 자원 저널이 있거나 저널을 "
            f"게시할 수 없습니다: {exc}"
        ) from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _remove_measurement_journal(path: Path) -> None:
    try:
        path.unlink()
        directory = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        raise InfrastructureUnavailable(
            f"이미지 측정 자원 저널을 제거할 수 없습니다: {exc}"
        ) from exc


def _load_measurement_journal(path: Path) -> Mapping[str, Any]:
    try:
        parent = validate_operator_directory(
            path.parent,
            "이미지 측정 저널 상위 경로",
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
            f"이미지 측정 자원 저널을 열 수 없습니다: {exc}"
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
                "이미지 측정 자원 저널 파일이 안전하지 않습니다."
            )
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            payload = stream.read(IMAGE_SIZE_EVIDENCE_MAX_BYTES + 1)
    finally:
        os.close(descriptor)
    value = _decode_json_object(payload, "이미지 측정 자원 저널")
    expected = {
        "schema_version",
        "report_type",
        "container_name",
        "attempt_token",
        "create_confirmed",
        "create_uncertain",
        "runtime_command",
        "daemon_id",
    }
    if set(value) != expected:
        raise InfrastructureUnavailable(
            "이미지 측정 자원 저널 필드가 올바르지 않습니다."
        )
    runtime_value = value["runtime_command"]
    if (
        isinstance(value["schema_version"], bool)
        or value["schema_version"] != 1
        or value["report_type"]
        != "operator-image-measurement-resource-journal"
        or not isinstance(value["container_name"], str)
        or re.fullmatch(
            r"ossp-router-image-evidence-[a-zA-Z0-9-]{1,80}",
            value["container_name"],
        )
        is None
        or not isinstance(value["attempt_token"], str)
        or re.fullmatch(r"[0-9a-f]{32}", value["attempt_token"]) is None
        or not isinstance(value["create_confirmed"], bool)
        or not isinstance(value["create_uncertain"], bool)
        or not isinstance(runtime_value, list)
        or not runtime_value
        or any(
            not isinstance(item, str) or not item or len(item) > 1024
            for item in runtime_value
        )
        or not isinstance(value["daemon_id"], str)
        or re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}",
            value["daemon_id"],
        )
        is None
    ):
        raise InfrastructureUnavailable(
            "이미지 측정 자원 저널 값이 올바르지 않습니다."
        )
    return value


def reconcile_measurement_journal(
    runtime_command: Sequence[str],
    journal_path: Path,
    *,
    confirm_daemon_restarted: bool = False,
) -> None:
    """Label-check and remove only the resource named by a persisted journal."""

    journal_parent = validate_operator_directory(
        journal_path.parent,
        "이미지 측정 저널 상위 경로",
    )
    journal_path = journal_parent / journal_path.name
    value = _load_measurement_journal(journal_path)
    if tuple(value["runtime_command"]) != tuple(runtime_command):
        raise InfrastructureUnavailable(
            "이미지 측정 저널의 Docker 실행 명령·context가 현재와 다릅니다."
        )
    current_daemon_id = inspect_runtime_daemon_id(runtime_command)
    if current_daemon_id != value["daemon_id"]:
        if not confirm_daemon_restarted:
            raise InfrastructureUnavailable(
                "Docker daemon identity가 달라 자원 부재를 확정할 수 없습니다."
            )
        _remove_measurement_journal(journal_path)
        return
    name = value["container_name"]
    inspected, inspect_error = _run_control_command(
        [
            *runtime_command,
            "container",
            "inspect",
            "--format",
            "\n".join(
                (
                    "{{json .Id}}",
                    f'{{{{json (index .Config.Labels "{ATTEMPT_LABEL}")}}}}',
                )
            ),
            name,
        ],
        timeout_seconds=10,
        max_stdout_bytes=8192,
        max_stderr_bytes=4096,
    )
    if inspect_error is not None or inspected is None:
        raise InfrastructureUnavailable(
            _control_failure_detail(
                "보류된 이미지 측정 컨테이너를 확인할 수 없음",
                inspected,
                inspect_error,
            )
        )
    if inspected.returncode != 0:
        if not _control_target_absent(inspected):
            raise InfrastructureUnavailable(
                _control_failure_detail(
                    "보류된 이미지 측정 컨테이너 검사 실패",
                    inspected,
                    None,
                )
            )
        if value["create_uncertain"] and not confirm_daemon_restarted:
            raise InfrastructureUnavailable(
                "불확실한 Docker create 뒤 컨테이너 부재를 확정하려면 "
                "daemon 재시작 확인이 필요합니다."
            )
        _remove_measurement_journal(journal_path)
        return
    try:
        lines = inspected.stdout.decode("utf-8").splitlines()
        if len(lines) != 2:
            raise ValueError("inspect 줄 수")
        _container_id, observed_label = (json.loads(line) for line in lines)
    except (RecursionError, UnicodeError, ValueError) as exc:
        raise InfrastructureUnavailable(
            "보류된 이미지 측정 컨테이너 응답이 올바르지 않습니다."
        ) from exc
    expected_label = f"image-evidence-{value['attempt_token']}"
    if observed_label != expected_label:
        raise InfrastructureUnavailable(
            "보류된 컨테이너의 이미지 측정 시도 라벨이 다릅니다."
        )
    removed, remove_error = _run_control_command(
        [*runtime_command, "rm", "--force", name],
        timeout_seconds=30,
        max_stdout_bytes=4096,
        max_stderr_bytes=64 * 1024,
    )
    if remove_error is not None or removed is None or removed.returncode != 0:
        raise InfrastructureUnavailable(
            _control_failure_detail(
                "보류된 이미지 측정 컨테이너를 정리할 수 없음",
                removed,
                remove_error,
            )
        )
    _remove_measurement_journal(journal_path)


RootfsMeasurer = Callable[[Sequence[str], str, str], int]


def generate_image_size_evidence(
    *,
    runtime_command: Sequence[str],
    oci_layout: Path,
    submitted_digest: str,
    resource_journal_path: Optional[Path] = None,
    rootfs_measurer: Optional[RootfsMeasurer] = None,
) -> ImageSizeEvidence:
    """Generate evidence from verified OCI blobs and one cached platform image."""

    selected_digest, config_digest, compressed = measure_oci_layout(
        oci_layout,
        submitted_digest,
    )
    metadata = inspect_image_runtime_metadata(
        runtime_command,
        submitted_digest,
        platform=OFFICIAL_CONTAINER_PLATFORM,
    )
    validate_image_configuration(metadata)
    try:
        local_id = metadata["Id"]
        repository_digests = metadata["RepoDigests"]
        operating_system = metadata["Os"]
        architecture = metadata["Architecture"]
    except (KeyError, TypeError) as exc:
        raise InfrastructureUnavailable("로컬 이미지 검사 결과가 올바르지 않습니다.") from exc
    if (
        local_id not in {selected_digest, config_digest}
        or not isinstance(repository_digests, list)
        or submitted_digest not in repository_digests
        or operating_system != OFFICIAL_CONTAINER_OS
        or architecture != OFFICIAL_CONTAINER_ARCHITECTURE
    ):
        raise InfrastructureUnavailable(
            f"OCI layout과 로컬 {OFFICIAL_CONTAINER_PLATFORM} 이미지의 "
            "대응을 확인할 수 없습니다."
        )
    if rootfs_measurer is None:
        if resource_journal_path is None:
            raise InfrastructureUnavailable(
                "Docker rootfs 측정에는 영속 자원 저널 경로가 필요합니다."
            )
        journal_parent = validate_operator_directory(
            resource_journal_path.parent,
            "이미지 측정 저널 상위 경로",
        )
        resource_journal_path = (
            journal_parent / resource_journal_path.name
        )
        rootfs = _measure_export_tar(
            runtime_command,
            submitted_digest,
            OFFICIAL_CONTAINER_PLATFORM,
            resource_journal_path,
        )
    else:
        rootfs = rootfs_measurer(
            runtime_command,
            submitted_digest,
            OFFICIAL_CONTAINER_PLATFORM,
        )
    provisional = ImageSizeEvidence(
        submitted_digest=submitted_digest,
        selected_manifest_digest=selected_digest,
        config_digest=config_digest,
        operating_system=operating_system,
        architecture=architecture,
        oci_compressed_layer_bytes=compressed,
        rootfs_apparent_bytes=rootfs,
        oci_layer_measurement_method=OCI_LAYER_MEASUREMENT_METHOD,
        rootfs_measurement_method=ROOTFS_MEASUREMENT_METHOD,
        evidence_sha256="0" * 64,
    )
    validate_image_size_evidence(
        provisional,
        submitted_digest=submitted_digest,
        local_image_id=local_id,
        operating_system=operating_system,
        architecture=architecture,
    )
    return replace(
        provisional,
        evidence_sha256=hashlib.sha256(
            _evidence_to_payload(provisional)
        ).hexdigest(),
    )


def _evidence_to_payload(evidence: ImageSizeEvidence) -> bytes:
    value = {
        "schema_version": 1,
        "report_type": "operator-image-size-evidence",
        "submitted_digest": evidence.submitted_digest,
        "selected_manifest_digest": evidence.selected_manifest_digest,
        "config_digest": evidence.config_digest,
        "os": evidence.operating_system,
        "architecture": evidence.architecture,
        "oci_compressed_layer_bytes": evidence.oci_compressed_layer_bytes,
        "rootfs_apparent_bytes": evidence.rootfs_apparent_bytes,
        "oci_layer_measurement_method": evidence.oci_layer_measurement_method,
        "rootfs_measurement_method": evidence.rootfs_measurement_method,
    }
    payload = dumps_json(value).encode("utf-8")
    if len(payload) > IMAGE_SIZE_EVIDENCE_MAX_BYTES:
        raise InfrastructureUnavailable("이미지 크기 증거가 파일 상한을 초과합니다.")
    return payload


def write_image_size_evidence(path: Path, evidence: ImageSizeEvidence) -> str:
    """Atomically publish one 0600 evidence file in an operator 0700 directory."""

    parent = validate_operator_directory(
        path.parent,
        "증거 출력 상위 경로",
    )
    path = parent / path.name
    if path.exists() or path.is_symlink():
        raise InfrastructureUnavailable("기존 이미지 크기 증거를 덮어쓰지 않습니다.")
    payload = _evidence_to_payload(evidence)
    temporary = parent / f".{path.name}.operator-{secrets.token_hex(8)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(str(temporary), flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)
    try:
        os.link(temporary, path, follow_symlinks=False)
        temporary.unlink()
        directory = os.open(str(parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return hashlib.sha256(payload).hexdigest()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="router-measure-image",
        description="OCI layout과 로컬 이미지에서 공식 크기 증거를 생성합니다.",
    )
    parser.add_argument("--oci-layout", type=Path)
    parser.add_argument("--image")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runtime", default="docker")
    parser.add_argument("--docker-context")
    parser.add_argument("--cleanup-pending", action="store_true")
    parser.add_argument("--confirm-daemon-restarted", action="store_true")
    args = parser.parse_args(argv)
    runtime = [args.runtime]
    if args.docker_context:
        runtime.extend(["--context", args.docker_context])
    try:
        journal_path = args.output.parent / _MEASUREMENT_JOURNAL_NAME
        if args.cleanup_pending:
            reconcile_measurement_journal(
                runtime,
                journal_path,
                confirm_daemon_restarted=args.confirm_daemon_restarted,
            )
            print("OK: pending image measurement resource reconciled")
            return 0
        if args.oci_layout is None or args.image is None:
            raise ValueError(
                "증거 생성에는 --oci-layout과 --image가 필요합니다."
            )
        evidence = generate_image_size_evidence(
            runtime_command=runtime,
            oci_layout=args.oci_layout,
            submitted_digest=args.image,
            resource_journal_path=journal_path,
        )
        digest = write_image_size_evidence(args.output, evidence)
    except ImagePreflightRejected as exc:
        print(f"ERROR: {exc}", file=os.sys.stderr)
        return 6
    except (InfrastructureUnavailable, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=os.sys.stderr)
        return 4
    print(f"OK: image size evidence sha256:{digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
