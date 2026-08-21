# SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from ossp_router.image_evidence import (
    _BoundedExportReader,
    _measure_export_tar,
    generate_image_size_evidence,
    main as image_evidence_main,
    measure_oci_layout,
    reconcile_measurement_journal,
    write_image_size_evidence,
)
from ossp_router.protocol import load_bundled_policy, load_input, write_json
from ossp_router.orchestrator import (
    AuditStatus,
    ResolvedImage,
    resolve_submitted_image,
    run_official_tier,
)
from ossp_router.runtime import (
    IMAGE_SIZE_EVIDENCE_MAX_BYTES,
    OFFICIAL_RESOURCE_LIMITS,
    RESOURCE_JOURNAL_NAME,
    AttemptKind,
    AttemptResult,
    DeclaredVolumeNotAllowed,
    ImageLimitExceeded,
    ImageSizeEvidence,
    InfrastructureUnavailable,
    ResourceLockUnavailable,
    _ContainerStateSnapshot,
    _ResourceJournal,
    _bounded_log,
    _inspect_participant_container,
    _load_resource_journal,
    _reconcile_resource_journal,
    _reconcile_and_remove_participant_container,
    _resource_journal_path,
    _run_control_command,
    _start_output_helper,
    _write_resource_journal,
    build_isolated_container_command,
    inspect_image_runtime_metadata,
    inspect_runtime_daemon_id,
    load_image_size_evidence,
    prepare_operator_directory,
    require_no_pending_runtime_resources,
    run_isolated_container_once,
    run_router_once,
    validate_host_core_dump_policy,
    validate_image_runtime_configuration,
    validate_operator_directory,
)


ROOT = pathlib.Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "container/Dockerfile"
BASE_REPOSITORY_DIGEST = (
    "python@sha256:"
    "f73754c398b259dfbbe482361dca8b464dea57da74efe5214966ca2ee767ee12"
)
TEST_DAEMON_ID = "phase-c-test-daemon"


def _json_bytes(value) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _write_oci_blob(layout: pathlib.Path, payload: bytes) -> str:
    digest = hashlib.sha256(payload).hexdigest()
    path = layout / "blobs" / "sha256" / digest
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return "sha256:" + digest


def _make_linux_arm64_oci_layout(
    target: pathlib.Path,
    *,
    declared_volume: bool = False,
) -> tuple[str, str, str, pathlib.Path]:
    layout = target / "oci-layout"
    layout.mkdir()
    (layout / "oci-layout").write_bytes(
        _json_bytes({"imageLayoutVersion": "1.0.0"})
    )
    config_value = {"architecture": "arm64", "os": "linux"}
    if declared_volume:
        config_value["config"] = {
            "Volumes": {"/participant-controlled-secret-path": {}}
        }
    config = _json_bytes(config_value)
    config_digest = _write_oci_blob(layout, config)
    layer = b"bounded-compressed-layer"
    layer_digest = _write_oci_blob(layout, layer)
    manifest = _json_bytes(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {
                "mediaType": "application/vnd.oci.image.config.v1+json",
                "digest": config_digest,
                "size": len(config),
            },
            "layers": [
                {
                    "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                    "digest": layer_digest,
                    "size": len(layer),
                }
            ],
        }
    )
    manifest_digest = _write_oci_blob(layout, manifest)
    image_index = _json_bytes(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [
                {
                    "mediaType": (
                        "application/vnd.oci.image.manifest.v1+json"
                    ),
                    "digest": manifest_digest,
                    "size": len(manifest),
                    "platform": {"os": "linux", "architecture": "arm64"},
                }
            ],
        }
    )
    index_digest = _write_oci_blob(layout, image_index)
    (layout / "index.json").write_bytes(
        _json_bytes(
            {
                "schemaVersion": 2,
                "manifests": [
                    {
                        "mediaType": (
                            "application/vnd.oci.image.index.v1+json"
                        ),
                        "digest": index_digest,
                        "size": len(image_index),
                    }
                ],
            }
        )
    )
    submitted = "registry.example/router@" + index_digest
    return submitted, manifest_digest, config_digest, layout


def _docker_integration_enabled() -> bool:
    if os.environ.get("OSSP_RUN_CONTAINER_TESTS") != "1":
        return False
    if shutil.which("docker") is None:
        return False
    checked = subprocess.run(
        ["docker", "info"],
        capture_output=True,
        text=True,
        check=False,
    )
    return checked.returncode == 0


class RuntimeValidationTest(unittest.TestCase):
    def test_export_stream_limit_keeps_preflight_classification_after_exit(
        self,
    ) -> None:
        stream = mock.Mock()
        stream.read.return_value = b"x"
        process = mock.Mock(pid=12345)
        reader = _BoundedExportReader(stream, process)
        with mock.patch(
            "ossp_router.image_evidence._MAX_EXPORT_STREAM_BYTES",
            0,
        ), mock.patch(
            "ossp_router.image_evidence.os.killpg",
            side_effect=ProcessLookupError,
        ) as kill:
            with self.assertRaisesRegex(
                ImageLimitExceeded,
                "스트림",
            ):
                reader.read()
        kill.assert_called_once_with(12345, signal.SIGKILL)

    def test_image_evidence_cli_classifies_declared_volume_as_preflight(
        self,
    ) -> None:
        with mock.patch(
            "ossp_router.image_evidence.generate_image_size_evidence",
            side_effect=DeclaredVolumeNotAllowed("VOLUME"),
        ), mock.patch("sys.stderr"):
            returncode = image_evidence_main(
                [
                    "--oci-layout",
                    "/operator/oci",
                    "--image",
                    "registry.example/router@sha256:" + "a" * 64,
                    "--output",
                    "/operator/evidence/image.json",
                ]
            )
        self.assertEqual(6, returncode)

    def test_rootfs_measurement_cleans_up_late_create_after_timeout(
        self,
    ) -> None:
        calls = []
        observed_attempt_label = []

        def control(arguments, **_kwargs):
            calls.append(arguments)
            if "create" in arguments:
                observed_attempt_label.append(
                    arguments[arguments.index("--label") + 1].split("=", 1)[1]
                )
                return None, "create timeout"
            if "inspect" in arguments and sum(
                "inspect" in call for call in calls
            ) == 1:
                return subprocess.CompletedProcess(arguments, 1, b"", b""), None
            if "inspect" in arguments:
                return subprocess.CompletedProcess(
                    arguments,
                    0,
                    (
                        json.dumps("sha256:" + "a" * 64)
                        + "\n"
                        + json.dumps(observed_attempt_label[0])
                        + "\n"
                    ).encode("utf-8"),
                    b"",
                ), None
            if "rm" in arguments:
                return subprocess.CompletedProcess(arguments, 0, b"", b""), None
            raise AssertionError(arguments)

        with mock.patch(
            "ossp_router.image_evidence._run_control_command",
            side_effect=control,
        ), mock.patch(
            "ossp_router.image_evidence.inspect_runtime_daemon_id",
            return_value="test-daemon",
        ), mock.patch("ossp_router.image_evidence.time.sleep"):
            with tempfile.TemporaryDirectory() as temporary:
                target = pathlib.Path(temporary)
                target.chmod(0o700)
                journal = target / ".measurement-journal.json"
                with self.assertRaisesRegex(
                    InfrastructureUnavailable,
                    "create timeout",
                ):
                    _measure_export_tar(
                        ("docker",),
                        "registry.example/router@sha256:" + "b" * 64,
                        "linux/arm64",
                        journal,
                    )
                self.assertFalse(journal.exists())
        create = next(call for call in calls if "create" in call)
        self.assertEqual(
            ["--platform", "linux/arm64"],
            create[create.index("create") + 1:create.index("--name")],
        )
        self.assertIn(
            "registry.example/router@sha256:" + "b" * 64,
            create,
        )
        self.assertTrue(any("rm" in call for call in calls))

    def test_uncertain_rootfs_create_retains_persistent_journal(
        self,
    ) -> None:
        def control(arguments, **_kwargs):
            if "create" in arguments:
                return None, "create timeout"
            if "inspect" in arguments:
                return subprocess.CompletedProcess(arguments, 1, b"", b""), None
            raise AssertionError(arguments)

        with tempfile.TemporaryDirectory() as temporary:
            target = pathlib.Path(temporary)
            target.chmod(0o700)
            journal = target / ".measurement-journal.json"
            with mock.patch(
                "ossp_router.image_evidence._run_control_command",
                side_effect=control,
            ), mock.patch(
                "ossp_router.image_evidence.inspect_runtime_daemon_id",
                return_value="test-daemon",
            ), mock.patch("ossp_router.image_evidence.time.sleep"):
                with self.assertRaisesRegex(
                    InfrastructureUnavailable,
                    "운영자 확인과 정리",
                ):
                    _measure_export_tar(
                        ("docker",),
                        "registry.example/router@sha256:" + "b" * 64,
                        "linux/arm64",
                        journal,
                    )
            value = json.loads(journal.read_text(encoding="utf-8"))
            self.assertFalse(value["create_confirmed"])
            self.assertTrue(value["create_uncertain"])
            self.assertRegex(
                value["container_name"],
                r"^ossp-router-image-evidence-",
            )

            def reconcile_control(arguments, **_kwargs):
                if "inspect" in arguments:
                    payload = (
                        json.dumps("sha256:" + "a" * 64)
                        + "\n"
                        + json.dumps(
                            "image-evidence-" + value["attempt_token"]
                        )
                        + "\n"
                    ).encode("utf-8")
                    return subprocess.CompletedProcess(
                        arguments,
                        0,
                        payload,
                        b"",
                    ), None
                if "rm" in arguments:
                    return subprocess.CompletedProcess(
                        arguments,
                        0,
                        b"",
                        b"",
                    ), None
                raise AssertionError(arguments)

            with mock.patch(
                "ossp_router.image_evidence._run_control_command",
                side_effect=reconcile_control,
            ), mock.patch(
                "ossp_router.image_evidence.inspect_runtime_daemon_id",
                return_value="test-daemon",
            ):
                reconcile_measurement_journal(("docker",), journal)
            self.assertFalse(journal.exists())

    def test_nonzero_rootfs_create_absence_retains_journal(self) -> None:
        def control(arguments, **_kwargs):
            if "create" in arguments:
                return subprocess.CompletedProcess(
                    arguments,
                    1,
                    b"",
                    b"daemon returned nonzero",
                ), None
            if "inspect" in arguments:
                return subprocess.CompletedProcess(
                    arguments,
                    1,
                    b"",
                    b"no such container",
                ), None
            raise AssertionError(arguments)

        with tempfile.TemporaryDirectory() as temporary:
            target = pathlib.Path(temporary)
            target.chmod(0o700)
            journal = target / ".measurement-journal.json"
            with mock.patch(
                "ossp_router.image_evidence._run_control_command",
                side_effect=control,
            ), mock.patch(
                "ossp_router.image_evidence.inspect_runtime_daemon_id",
                return_value="test-daemon",
            ), mock.patch("ossp_router.image_evidence.time.sleep"):
                with self.assertRaisesRegex(
                    InfrastructureUnavailable,
                    "운영자 확인과 정리",
                ):
                    _measure_export_tar(
                        ("docker",),
                        "registry.example/router@sha256:" + "b" * 64,
                        "linux/arm64",
                        journal,
                    )
            value = json.loads(journal.read_text(encoding="utf-8"))
            self.assertFalse(value["create_confirmed"])
            self.assertTrue(value["create_uncertain"])

    def test_measurement_journal_reconcile_rejects_label_mismatch(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = pathlib.Path(temporary)
            target.chmod(0o700)
            journal = target / ".measurement-journal.json"
            journal.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "report_type": (
                            "operator-image-measurement-resource-journal"
                        ),
                        "container_name": (
                            "ossp-router-image-evidence-1234-abcdef"
                        ),
                        "attempt_token": "a" * 32,
                        "create_confirmed": True,
                        "create_uncertain": False,
                        "runtime_command": ["docker"],
                        "daemon_id": "test-daemon",
                    }
                ),
                encoding="utf-8",
            )
            journal.chmod(0o600)
            inspected = subprocess.CompletedProcess(
                ("docker",),
                0,
                (
                    json.dumps("sha256:" + "b" * 64)
                    + "\n"
                    + json.dumps("image-evidence-" + "c" * 32)
                    + "\n"
                ).encode("utf-8"),
                b"",
            )
            with mock.patch(
                "ossp_router.image_evidence._run_control_command",
                return_value=(inspected, None),
            ) as control, mock.patch(
                "ossp_router.image_evidence.inspect_runtime_daemon_id",
                return_value="test-daemon",
            ):
                with self.assertRaisesRegex(
                    InfrastructureUnavailable,
                    "라벨",
                ):
                    reconcile_measurement_journal(("docker",), journal)
            self.assertTrue(journal.exists())
            self.assertFalse(
                any("rm" in call.args[0] for call in control.call_args_list)
            )

    def test_measurement_journal_keeps_generic_inspect_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = pathlib.Path(temporary)
            target.chmod(0o700)
            journal = target / ".measurement-journal.json"
            journal.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "report_type": (
                            "operator-image-measurement-resource-journal"
                        ),
                        "container_name": (
                            "ossp-router-image-evidence-1234-abcdef"
                        ),
                        "attempt_token": "a" * 32,
                        "create_confirmed": True,
                        "create_uncertain": False,
                        "runtime_command": ["docker"],
                        "daemon_id": "test-daemon",
                    }
                ),
                encoding="utf-8",
            )
            journal.chmod(0o600)
            inspected = subprocess.CompletedProcess(
                ("docker",),
                1,
                b"",
                b"permission denied",
            )
            with mock.patch(
                "ossp_router.image_evidence._run_control_command",
                return_value=(inspected, None),
            ), mock.patch(
                "ossp_router.image_evidence.inspect_runtime_daemon_id",
                return_value="test-daemon",
            ):
                with self.assertRaisesRegex(
                    InfrastructureUnavailable,
                    "검사 실패",
                ):
                    reconcile_measurement_journal(("docker",), journal)
            self.assertTrue(journal.exists())

    def test_oci_layout_measurement_verifies_selected_manifest_and_blobs(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = pathlib.Path(temporary)
            submitted, manifest_digest, config_digest, layout = (
                _make_linux_arm64_oci_layout(target)
            )
            measured_manifest, measured_config, compressed = (
                measure_oci_layout(layout, submitted)
            )
        self.assertEqual(manifest_digest, measured_manifest)
        self.assertEqual(config_digest, measured_config)
        self.assertEqual(len(b"bounded-compressed-layer"), compressed)

    def test_oci_layout_measurement_rejects_tampered_layer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = pathlib.Path(temporary)
            submitted, _manifest, _config, layout = (
                _make_linux_arm64_oci_layout(target)
            )
            layer = b"bounded-compressed-layer"
            layer_path = (
                layout
                / "blobs"
                / "sha256"
                / hashlib.sha256(layer).hexdigest()
            )
            layer_path.write_bytes(b"x" * len(layer))
            with self.assertRaisesRegex(
                InfrastructureUnavailable,
                "SHA-256",
            ):
                measure_oci_layout(layout, submitted)

    def test_oci_layout_rejects_declared_volume_without_path_leak(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = pathlib.Path(temporary)
            submitted, _manifest, _config, layout = (
                _make_linux_arm64_oci_layout(
                    target,
                    declared_volume=True,
                )
            )
            with self.assertRaisesRegex(
                DeclaredVolumeNotAllowed,
                "VOLUME",
            ) as raised:
                measure_oci_layout(layout, submitted)
            self.assertNotIn(
                "/participant-controlled-secret-path",
                str(raised.exception),
            )

    def test_operator_generator_binds_oci_layout_local_image_and_rootfs(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            for observed_id_kind in ("config", "manifest"):
                with self.subTest(observed_id_kind=observed_id_kind):
                    target = root / observed_id_kind
                    target.mkdir()
                    submitted, manifest_digest, config_digest, layout = (
                        _make_linux_arm64_oci_layout(target)
                    )
                    observed_id = (
                        config_digest
                        if observed_id_kind == "config"
                        else manifest_digest
                    )
                    metadata = {
                        "Id": observed_id,
                        "RepoDigests": [submitted],
                        "Os": "linux",
                        "Architecture": "arm64",
                        "Config": {"Volumes": None},
                    }
                    measured = []

                    def rootfs_measurer(runtime, image, platform):
                        measured.append((runtime, image, platform))
                        return 456

                    with mock.patch(
                        "ossp_router.image_evidence."
                        "inspect_image_runtime_metadata",
                        return_value=metadata,
                    ) as inspect:
                        evidence = generate_image_size_evidence(
                            runtime_command=("docker",),
                            oci_layout=layout,
                            submitted_digest=submitted,
                            rootfs_measurer=rootfs_measurer,
                        )
                    inspect.assert_called_once_with(
                        ("docker",),
                        submitted,
                        platform="linux/arm64",
                    )
                    self.assertEqual(
                        [(("docker",), submitted, "linux/arm64")],
                        measured,
                    )
                    self.assertEqual(
                        manifest_digest,
                        evidence.selected_manifest_digest,
                    )
                    self.assertEqual(config_digest, evidence.config_digest)
                    self.assertEqual(456, evidence.rootfs_apparent_bytes)

                    evidence_directory = target / "evidence"
                    evidence_directory.mkdir(mode=0o700)
                    evidence_path = (
                        evidence_directory / "linux-arm64.json"
                    )
                    written_sha256 = write_image_size_evidence(
                        evidence_path,
                        evidence,
                    )
                    loaded = load_image_size_evidence(evidence_path)
                    self.assertEqual(
                        written_sha256,
                        loaded.evidence_sha256,
                    )
                    self.assertEqual(config_digest, loaded.config_digest)
                    self.assertEqual(
                        0o600,
                        evidence_path.stat().st_mode & 0o777,
                    )

    def test_image_size_evidence_loader_requires_operator_private_file(
        self,
    ) -> None:
        value = {
            "schema_version": 1,
            "report_type": "operator-image-size-evidence",
            "submitted_digest": (
                "registry.example/router@sha256:" + "a" * 64
            ),
            "selected_manifest_digest": "sha256:" + "b" * 64,
            "config_digest": "sha256:" + "c" * 64,
            "os": "linux",
            "architecture": "arm64",
            "oci_compressed_layer_bytes": 123,
            "rootfs_apparent_bytes": 456,
            "oci_layer_measurement_method": (
                "oci-manifest-layer-descriptors-v1"
            ),
            "rootfs_measurement_method": (
                "docker-export-tar-apparent-size-v1"
            ),
        }
        with tempfile.TemporaryDirectory() as temporary:
            parent = pathlib.Path(temporary)
            parent.chmod(0o700)
            path = parent / "evidence.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            path.chmod(0o600)
            evidence = load_image_size_evidence(path)
            self.assertEqual(123, evidence.oci_compressed_layer_bytes)
            self.assertEqual(456, evidence.rootfs_apparent_bytes)
            self.assertRegex(evidence.evidence_sha256, r"^[0-9a-f]{64}$")

            path.chmod(0o644)
            with self.assertRaisesRegex(
                InfrastructureUnavailable,
                "0600",
            ):
                load_image_size_evidence(path)

    def test_image_size_evidence_loader_rejects_unbounded_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = pathlib.Path(temporary)
            parent.chmod(0o700)
            path = parent / "evidence.json"
            path.write_bytes(b"{" + b" " * IMAGE_SIZE_EVIDENCE_MAX_BYTES)
            path.chmod(0o600)
            with self.assertRaisesRegex(
                InfrastructureUnavailable,
                "바이트 이하",
            ):
                load_image_size_evidence(path)

    def test_image_size_evidence_loader_rejects_duplicate_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = pathlib.Path(temporary)
            parent.chmod(0o700)
            path = parent / "evidence.json"
            path.write_text(
                '{"schema_version":1,"schema_version":1}',
                encoding="utf-8",
            )
            path.chmod(0o600)
            with self.assertRaisesRegex(
                InfrastructureUnavailable,
                "중복 JSON 키",
            ):
                load_image_size_evidence(path)

    def test_image_size_evidence_loader_rejects_boolean_schema_version(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = pathlib.Path(temporary)
            parent.chmod(0o700)
            path = parent / "evidence.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": True,
                        "report_type": "operator-image-size-evidence",
                        "submitted_digest": (
                            "registry.example/router@sha256:" + "a" * 64
                        ),
                        "selected_manifest_digest": "sha256:" + "b" * 64,
                        "config_digest": "sha256:" + "c" * 64,
                        "os": "linux",
                        "architecture": "arm64",
                        "oci_compressed_layer_bytes": 1,
                        "rootfs_apparent_bytes": 1,
                        "oci_layer_measurement_method": (
                            "oci-manifest-layer-descriptors-v1"
                        ),
                        "rootfs_measurement_method": (
                            "docker-export-tar-apparent-size-v1"
                        ),
                    }
                ),
                encoding="utf-8",
            )
            path.chmod(0o600)
            with self.assertRaisesRegex(
                InfrastructureUnavailable,
                "지원하지 않는",
            ):
                load_image_size_evidence(path)

    def test_external_json_recursion_is_bounded_infrastructure_error(
        self,
    ) -> None:
        deeply_nested = b"[" * 2000 + b"0" + b"]" * 2000
        with tempfile.TemporaryDirectory() as temporary:
            parent = pathlib.Path(temporary)
            parent.chmod(0o700)
            evidence_path = parent / "evidence.json"
            evidence_path.write_bytes(deeply_nested)
            evidence_path.chmod(0o600)
            with self.assertRaisesRegex(
                InfrastructureUnavailable,
                "JSON",
            ):
                load_image_size_evidence(evidence_path)

            layout_root = parent / "layout-root"
            layout_root.mkdir()
            submitted, _manifest, _config, layout = (
                _make_linux_arm64_oci_layout(layout_root)
            )
            (layout / "index.json").write_bytes(deeply_nested)
            with self.assertRaisesRegex(
                InfrastructureUnavailable,
                "JSON",
            ):
                measure_oci_layout(layout, submitted)

            runtime_output = parent / "runtime-output"
            runtime_output.mkdir()
            runtime_journal = runtime_output / RESOURCE_JOURNAL_NAME
            runtime_journal.write_bytes(deeply_nested)
            runtime_journal.chmod(0o600)
            with self.assertRaisesRegex(
                InfrastructureUnavailable,
                "저널",
            ):
                _load_resource_journal(runtime_output)

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.target = pathlib.Path(self.temporary.name)
        self.input_path = self.target / "inputs.json"
        write_json(
            self.input_path,
            {
                "schema_version": 1,
                "challenge_id": "phase-c-runtime",
                "split": "synthetic",
                "episodes": [
                    {"episode_id": "one", "prompt": "hello"},
                    {"episode_id": "two", "prompt": "prove 2 + 2 = 4"},
                ],
            },
        )
        self.inputs = load_input(self.input_path)
        self.policy = load_bundled_policy()
        self.output = self.target / "submission.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _run(self, command, *, timeout=2.0, maximum=8 * 1024 * 1024):
        return run_router_once(
            command,
            inputs=self.inputs,
            policy=self.policy,
            input_path=self.input_path,
            output_path=self.output,
            tier="fast",
            timeout_seconds=timeout,
            max_output_bytes=maximum,
        )

    def test_entrypoint_produces_one_valid_tier(self) -> None:
        result = self._run([sys.executable, "-m", "ossp_router.heuristic"])
        self.assertEqual(AttemptKind.VALID, result.kind, result.detail)
        self.assertEqual("fast", result.submission.tier)
        self.assertEqual(0o644, self.output.stat().st_mode & 0o777)

    def test_nonzero_exit_is_execution_failure(self) -> None:
        result = self._run([sys.executable, "-c", "raise SystemExit(7)"])
        self.assertEqual(AttemptKind.EXECUTION_FAILURE, result.kind)
        self.assertIn("7", result.detail)

    def test_timeout_is_execution_failure(self) -> None:
        result = self._run(
            [sys.executable, "-c", "import time; time.sleep(2)"],
            timeout=0.05,
        )
        self.assertEqual(AttemptKind.EXECUTION_FAILURE, result.kind)
        self.assertIn("시간 초과", result.detail)

    def test_timeout_does_not_wait_for_detached_descendant_log_handles(self) -> None:
        pid_path = self.target / "detached-timeout.pid"
        code = (
            "import pathlib,subprocess,sys,time;"
            "child=subprocess.Popen("
            "[sys.executable,'-c','import time; time.sleep(2)'],"
            "start_new_session=True);"
            f"pathlib.Path({str(pid_path)!r}).write_text("
            "str(child.pid),encoding='ascii');"
            "time.sleep(10)"
        )
        started = time.monotonic()
        try:
            result = run_router_once(
                [sys.executable, "-c", code],
                inputs=self.inputs,
                policy=self.policy,
                input_path=self.input_path,
                output_path=self.output,
                tier="fast",
                timeout_seconds=0.2,
                termination_grace_seconds=0.1,
            )
        finally:
            if pid_path.is_file():
                try:
                    os.kill(int(pid_path.read_text(encoding="ascii")), signal.SIGKILL)
                except ProcessLookupError:
                    pass
        elapsed = time.monotonic() - started
        self.assertEqual(AttemptKind.EXECUTION_FAILURE, result.kind)
        self.assertIn("시간 초과", result.detail)
        self.assertLess(elapsed, 1.5)

    def test_normal_exit_does_not_wait_for_detached_descendant_logs(self) -> None:
        pid_path = self.target / "detached-normal-exit.pid"
        code = (
            "import pathlib,subprocess,sys;"
            "child=subprocess.Popen("
            "[sys.executable,'-c','import time; time.sleep(2)'],"
            "start_new_session=True);"
            f"pathlib.Path({str(pid_path)!r}).write_text("
            "str(child.pid),encoding='ascii');"
            "raise SystemExit(7)"
        )
        started = time.monotonic()
        try:
            result = self._run([sys.executable, "-c", code], timeout=5.0)
        finally:
            if pid_path.is_file():
                try:
                    os.kill(int(pid_path.read_text(encoding="ascii")), signal.SIGKILL)
                except ProcessLookupError:
                    pass
        elapsed = time.monotonic() - started
        self.assertEqual(AttemptKind.EXECUTION_FAILURE, result.kind)
        self.assertEqual(7, result.returncode)
        self.assertLess(elapsed, 1.5)

    def test_logs_are_retained_in_bounded_memory(self) -> None:
        result = run_router_once(
            [
                sys.executable,
                "-c",
                (
                    "import sys;"
                    "sys.stdout.write('x'*4096);"
                    "sys.stderr.write('y'*4096);"
                    "raise SystemExit(7)"
                ),
            ],
            inputs=self.inputs,
            policy=self.policy,
            input_path=self.input_path,
            output_path=self.output,
            tier="fast",
            timeout_seconds=2.0,
            max_log_bytes=32,
        )
        self.assertEqual(AttemptKind.EXECUTION_FAILURE, result.kind)
        self.assertTrue(result.stdout.startswith("x" * 32))
        self.assertTrue(result.stderr.startswith("y" * 32))
        self.assertLessEqual(len(result.stdout.encode("utf-8")), 32)
        self.assertLessEqual(len(result.stderr.encode("utf-8")), 32)

    def test_invalid_utf8_log_stays_within_normalized_byte_limit(self) -> None:
        maximum = 256 * 1024
        normalized = _bounded_log(b"\xff" * maximum, maximum)
        self.assertLessEqual(len(normalized.encode("utf-8")), maximum)

    def test_process_invalid_utf8_log_stays_within_byte_limit(self) -> None:
        result = run_router_once(
            [
                sys.executable,
                "-c",
                "import sys;sys.stdout.buffer.write(b'\\xff'*32)",
            ],
            inputs=self.inputs,
            policy=self.policy,
            input_path=self.input_path,
            output_path=self.output,
            tier="fast",
            max_log_bytes=32,
            max_log_emitted_bytes=64,
        )
        self.assertLessEqual(len(result.stdout.encode("utf-8")), 32)

    def test_control_command_output_has_a_streaming_hard_limit(self) -> None:
        completed, error = _run_control_command(
            [
                sys.executable,
                "-c",
                "import sys;sys.stdout.buffer.write(b'x'*65537)",
            ],
            max_stdout_bytes=65536,
        )
        self.assertIsNone(completed)
        self.assertIn("한도", error)

    def test_negative_log_limit_is_rejected_before_launch(self) -> None:
        with self.assertRaisesRegex(ValueError, "max_log_bytes"):
            run_router_once(
                [str(self.target / "does-not-exist")],
                inputs=self.inputs,
                policy=self.policy,
                input_path=self.input_path,
                output_path=self.output,
                tier="fast",
                max_log_bytes=-2,
            )

    def test_log_emission_limit_stops_the_process(self) -> None:
        result = run_router_once(
            [
                sys.executable,
                "-c",
                "import sys,time;sys.stdout.write('x'*65);sys.stdout.flush();time.sleep(2)",
            ],
            inputs=self.inputs,
            policy=self.policy,
            input_path=self.input_path,
            output_path=self.output,
            tier="fast",
            timeout_seconds=1,
            max_log_bytes=32,
            max_log_emitted_bytes=64,
            termination_grace_seconds=0.1,
        )
        self.assertEqual(AttemptKind.EXECUTION_FAILURE, result.kind)
        self.assertIn("방출 한도", result.detail)
        self.assertLessEqual(len(result.stdout.encode("utf-8")), 32)

    def test_log_emission_limit_cannot_be_smaller_than_retention(self) -> None:
        with self.assertRaisesRegex(ValueError, "max_log_emitted_bytes"):
            run_router_once(
                [sys.executable, "-c", "pass"],
                inputs=self.inputs,
                policy=self.policy,
                input_path=self.input_path,
                output_path=self.output,
                tier="fast",
                max_log_bytes=65,
                max_log_emitted_bytes=64,
            )

    def test_missing_output_is_execution_failure(self) -> None:
        result = self._run([sys.executable, "-c", "pass"])
        self.assertEqual(AttemptKind.EXECUTION_FAILURE, result.kind)
        self.assertIn("생성되지", result.detail)

    def test_bad_json_is_execution_failure(self) -> None:
        code = (
            "import pathlib,sys;"
            "p=sys.argv[sys.argv.index('--output')+1];"
            "pathlib.Path(p).write_text('{bad',encoding='utf-8')"
        )
        result = self._run([sys.executable, "-c", code])
        self.assertEqual(AttemptKind.EXECUTION_FAILURE, result.kind)
        self.assertIn("출력 검증 실패", result.detail)

    def test_missing_decision_is_execution_failure(self) -> None:
        value = {
            "schema_version": 1,
            "challenge_id": "phase-c-runtime",
            "policy_id": self.policy.policy_id,
            "split": "synthetic",
            "tier": "fast",
            "decisions": [{"episode_id": "one", "model_id": "ax31-light"}],
        }
        payload = json.dumps(value)
        code = (
            "import pathlib,sys;"
            "p=sys.argv[sys.argv.index('--output')+1];"
            f"pathlib.Path(p).write_text({payload!r},encoding='utf-8')"
        )
        result = self._run([sys.executable, "-c", code])
        self.assertEqual(AttemptKind.EXECUTION_FAILURE, result.kind)
        self.assertIn("누락", result.detail)

    def test_oversized_output_is_execution_failure(self) -> None:
        code = (
            "import pathlib,sys;"
            "p=sys.argv[sys.argv.index('--output')+1];"
            "pathlib.Path(p).write_text('x'*100,encoding='utf-8')"
        )
        result = self._run([sys.executable, "-c", code], maximum=32)
        self.assertEqual(AttemptKind.EXECUTION_FAILURE, result.kind)
        self.assertIn("한도", result.detail)

    def test_growing_output_is_stopped_before_wall_timeout(self) -> None:
        code = (
            "import pathlib,sys,time;"
            "p=sys.argv[sys.argv.index('--output')+1];"
            "pathlib.Path(p).write_text('x'*10000,encoding='utf-8');"
            "time.sleep(10)"
        )
        started = time.monotonic()
        result = self._run(
            [sys.executable, "-c", code],
            timeout=5,
            maximum=32,
        )
        elapsed = time.monotonic() - started
        self.assertEqual(AttemptKind.EXECUTION_FAILURE, result.kind)
        self.assertIn("한도", result.detail)
        self.assertLess(elapsed, 1.5)

    def test_operator_cannot_start_command_is_infrastructure_failure(self) -> None:
        result = self._run([str(self.target / "does-not-exist")])
        self.assertEqual(AttemptKind.INFRASTRUCTURE_FAILURE, result.kind)

    def test_dockerfile_pins_multi_platform_base_and_nonroot_user(self) -> None:
        text = DOCKERFILE.read_text(encoding="utf-8")
        match = re.search(
            r"^FROM python:3\.11\.15-alpine3\.23@sha256:([0-9a-f]{64})$",
            text,
            re.MULTILINE,
        )
        self.assertIsNotNone(match)
        self.assertIn("USER 65532:65532", text)
        self.assertNotIn("pip install", text)
        self.assertNotRegex(text, r"\b(?:ADD|curl|wget)\b")

    def test_official_limits_match_report_and_runtime_docs(self) -> None:
        limits = OFFICIAL_RESOURCE_LIMITS
        self.assertEqual(2, limits.cpu_cores)
        self.assertEqual(90, limits.wall_time_seconds)
        self.assertEqual(2 * 1024**3, limits.memory_bytes)
        self.assertEqual(2 * 1024**3, limits.memory_swap_total_bytes)
        self.assertEqual(32, limits.processes_and_threads_total)
        self.assertEqual(256 * 1024**2, limits.temporary_storage_bytes)
        self.assertEqual(4 * 1024**2, limits.output_bytes)
        self.assertEqual(64, limits.output_inodes)
        self.assertEqual(256 * 1024, limits.retained_log_bytes_per_stream)
        self.assertEqual(1024**2, limits.emitted_log_bytes_per_stream)
        self.assertEqual(1024**3, limits.oci_compressed_image_bytes)
        self.assertEqual(2 * 1024**3, limits.rootfs_apparent_bytes)

        report = json.loads(
            (ROOT / "docs/runtime-benchmark.json").read_text(encoding="utf-8")
        )
        proposal = report["resource_limit_proposal"]
        self.assertEqual(3, report["schema_version"])
        self.assertEqual(
            "final-frozen",
            proposal["status"],
        )
        self.assertEqual("per-tier-container-run", proposal["scope"])
        self.assertEqual(limits.cpu_cores, proposal["cpu_cores"])
        self.assertEqual(
            limits.wall_time_seconds,
            proposal["wall_time_seconds"],
        )
        self.assertEqual(limits.memory_bytes, proposal["memory_bytes"])
        self.assertEqual(
            limits.memory_swap_total_bytes,
            proposal["memory_swap_total_bytes"],
        )
        self.assertEqual(
            limits.processes_and_threads_total,
            proposal["processes_and_threads_total"],
        )
        self.assertEqual(
            limits.temporary_storage_bytes,
            proposal["temporary_storage_bytes"],
        )
        self.assertEqual(limits.output_bytes, proposal["output_bytes"])
        self.assertEqual(limits.output_inodes, proposal["output_inodes"])
        self.assertEqual(
            limits.retained_log_bytes_per_stream,
            proposal["retained_log_bytes_per_stream"],
        )
        self.assertEqual(
            limits.emitted_log_bytes_per_stream,
            proposal["emitted_log_bytes_per_stream"],
        )
        self.assertEqual(
            limits.oci_compressed_image_bytes,
            proposal["oci_compressed_image_limit_bytes"],
        )
        self.assertEqual(
            limits.rootfs_apparent_bytes,
            proposal["rootfs_apparent_limit_bytes"],
        )
        platform_proposal = proposal["platform_proposal"]
        self.assertEqual("final", platform_proposal["status"])
        self.assertTrue(
            platform_proposal[
                "apple_silicon_colima_benchmark_completed"
            ]
        )
        self.assertEqual("arm64", platform_proposal["architecture"])
        self.assertTrue(
            proposal["validation"][
                "representative_router_validation_completed"
            ]
        )
        self.assertTrue(
            proposal["validation"][
                "final_runtime_boundary_validation_completed"
            ]
        )
        self.assertEqual("arm64", report["environment"]["machine"])

        runtime_text = (ROOT / "docs/RUNTIME.md").read_text(encoding="utf-8")
        for phrase in (
            "--cpus 2",
            "--memory 2g",
            "--memory-swap 2g",
            "--pids-limit 32",
            "size=256m",
            "등급별 실행 시간 90초",
        ):
            self.assertIn(phrase, runtime_text)
        self.assertIn("최종 자원 한도", runtime_text)
        self.assertIn("Apple Silicon 장비의 Colima Linux VM", runtime_text)

    def test_isolated_container_command_uses_candidate_profile(self) -> None:
        command = build_isolated_container_command(
            ("docker", "--context", "colima"),
            image="registry.example/router@sha256:" + "a" * 64,
            input_path=self.input_path,
            output_volume="ossp-router-output-test",
            container_name="ossp-router-participant-test",
            attempt_token="b" * 32,
            tier="balanced",
        )
        self.assertEqual(
            [
                "docker",
                "--context",
                "colima",
                "run",
                "--pull",
                "never",
                "--log-driver",
                "none",
                "--stop-timeout",
                "5",
                "--stop-signal",
                "SIGTERM",
                "--no-healthcheck",
                "--name",
                "ossp-router-participant-test",
                "--label",
                "org.sktelecom.ossp-router.attempt=" + "b" * 32,
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
                "65532:65532",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--cpus",
                "2",
                "--memory",
                "2g",
                "--memory-swap",
                "2g",
                "--pids-limit",
                "32",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=256m",
                "--mount",
                (
                    f"type=bind,src={self.input_path.resolve()},"
                    "dst=/challenge/input/inputs.json,readonly"
                ),
                "--mount",
                (
                    "type=volume,src=ossp-router-output-test,"
                    "dst=/challenge/output,volume-nocopy"
                ),
                "registry.example/router@sha256:" + "a" * 64,
                "--input",
                "/challenge/input/inputs.json",
                "--tier",
                "balanced",
                "--output",
                "/challenge/output/submission.json",
            ],
            command,
        )

    def test_isolated_container_command_pins_explicit_platform(self) -> None:
        command = build_isolated_container_command(
            ("docker",),
            image="registry.example/router@sha256:" + "a" * 64,
            input_path=self.input_path,
            output_volume="ossp-router-output-platform",
            container_name="ossp-router-participant-platform",
            attempt_token="c" * 32,
            tier="fast",
            platform="linux/arm64",
        )
        self.assertEqual(
            ["--platform", "linux/arm64"],
            command[
                command.index("--label") + 2:command.index("--network")
            ],
        )

    def test_isolated_container_command_rejects_unknown_tier(self) -> None:
        with self.assertRaisesRegex(ValueError, "알 수 없는 실행 등급"):
            build_isolated_container_command(
                ("docker",),
                image="registry.example/router@sha256:" + "a" * 64,
                input_path=self.input_path,
                output_volume="ossp-router-output-test",
                container_name="ossp-router-participant-test",
                attempt_token="b" * 32,
                tier="unknown",
            )

    def test_isolated_container_command_rejects_mutable_image_tag(self) -> None:
        with self.assertRaisesRegex(ValueError, "sha256"):
            build_isolated_container_command(
                ("docker",),
                image="router:latest",
                input_path=self.input_path,
                output_volume="ossp-router-output-test",
                container_name="ossp-router-participant-test",
                attempt_token="b" * 32,
                tier="fast",
            )

    def test_host_core_dump_preflight_rejects_pipe_collector(self) -> None:
        completed = subprocess.CompletedProcess(
            ("docker",),
            0,
            b"|/usr/lib/systemd/systemd-coredump\n",
            b"",
        )
        with mock.patch(
            "ossp_router.runtime._run_control_command",
            return_value=(completed, None),
        ) as run:
            with self.assertRaisesRegex(
                InfrastructureUnavailable,
                "파이프 수집",
            ):
                validate_host_core_dump_policy(
                    ("docker",),
                    "ossp-router-helper-test",
                )
        command = run.call_args.args[0]
        self.assertEqual(
            ["docker", "exec", "ossp-router-helper-test"],
            command[:3],
        )
        self.assertNotIn("run", command[:3])

    def test_host_core_dump_preflight_accepts_file_pattern(self) -> None:
        completed = subprocess.CompletedProcess(
            ("docker",),
            0,
            b"core\n",
            b"",
        )
        with mock.patch(
            "ossp_router.runtime._run_control_command",
            return_value=(completed, None),
        ):
            validate_host_core_dump_policy(
                ("docker",),
                "ossp-router-helper-test",
            )

    def test_image_inspect_requests_only_volume_presence(self) -> None:
        completed = subprocess.CompletedProcess(
            ("docker",),
            0,
            (
                b'"sha256:' + b"a" * 64 + b'"\n'
                b'["registry.example/router@sha256:' + b"b" * 64 + b'"]\n'
                b'"linux"\n'
                b'"arm64"\n'
                b"true\n"
            ),
            b"",
        )
        with mock.patch(
            "ossp_router.runtime._run_control_command",
            return_value=(completed, None),
        ) as run:
            metadata = inspect_image_runtime_metadata(
                ("docker",),
                "sha256:" + "a" * 64,
                platform="linux/arm64",
            )
        self.assertTrue(metadata["Config"]["Volumes"])
        template = run.call_args.args[0][
            run.call_args.args[0].index("--format") + 1
        ]
        self.assertIn('if (index .Config "Volumes")', template)
        self.assertNotIn('json (index .Config "Volumes")', template)
        command = run.call_args.args[0]
        self.assertEqual(
            ["--platform", "linux/arm64"],
            command[
                command.index("inspect") + 1:command.index("--format")
            ],
        )

    def test_runtime_daemon_id_is_read_with_a_bounded_info_query(self) -> None:
        completed = subprocess.CompletedProcess(
            ("docker",),
            0,
            b'"phase-c-engine-id"\n',
            b"",
        )
        with mock.patch(
            "ossp_router.runtime._run_control_command",
            return_value=(completed, None),
        ) as run:
            daemon_id = inspect_runtime_daemon_id(
                ("docker", "--context", "colima"),
            )
        self.assertEqual("phase-c-engine-id", daemon_id)
        self.assertEqual(
            [
                "docker",
                "--context",
                "colima",
                "info",
                "--format",
                "{{json .ID}}",
            ],
            run.call_args.args[0],
        )
        self.assertEqual(1024, run.call_args.kwargs["max_stdout_bytes"])

    def test_container_inspect_requests_only_runtime_error_presence(self) -> None:
        completed = subprocess.CompletedProcess(
            ("docker",),
            0,
            (
                b'"exited"\n0\ntrue\nfalse\n'
                b'"2026-01-01T00:00:00Z"\n'
                b'"' + b"b" * 32 + b'"\n'
                b'"' + b"a" * 64 + b'"\n'
            ),
            b"",
        )
        with mock.patch(
            "ossp_router.runtime._run_control_command",
            return_value=(completed, None),
        ) as run:
            snapshot, error, absent = _inspect_participant_container(
                ("docker",),
                "ossp-router-participant-test",
            )
        self.assertIsNone(error)
        self.assertFalse(absent)
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertTrue(snapshot.runtime_error)
        template = run.call_args.args[0][
            run.call_args.args[0].index("--format") + 1
        ]
        self.assertIn("if .State.Error", template)
        self.assertNotIn("json .State.Error", template)

    def test_output_helper_disables_core_dumps(self) -> None:
        completed = subprocess.CompletedProcess(("docker",), 0, b"id\n", b"")
        with mock.patch(
            "ossp_router.runtime._run_control_command",
            return_value=(completed, None),
        ) as run, mock.patch(
            "ossp_router.runtime._inspect_attempt_label",
            return_value=("b" * 32, None, False),
        ):
            error = _start_output_helper(
                ("docker",),
                helper_name="ossp-router-helper-test",
                volume_name="ossp-router-output-test",
                result_directory=self.target,
                attempt_token="b" * 32,
            )
        self.assertIsNone(error)
        command = run.call_args.args[0]
        self.assertIn("--ulimit", command)
        self.assertIn("core=0:0", command)

    def test_resource_journal_waits_for_late_resources_and_reconciles(self) -> None:
        token = "c" * 32
        journal = _ResourceJournal(
            attempt_token=token,
            participant_name=f"ossp-router-participant-{token}",
            helper_name="ossp-router-helper-late",
            volume_name="ossp-router-output-late",
            runtime_command=("docker",),
            daemon_id=TEST_DAEMON_ID,
        )
        _write_resource_journal(self.target, journal)
        self.assertEqual(
            RESOURCE_JOURNAL_NAME,
            _resource_journal_path(self.target).name,
        )

        with mock.patch(
            "ossp_router.runtime.inspect_runtime_daemon_id",
            return_value=TEST_DAEMON_ID,
        ), mock.patch(
            "ossp_router.runtime._inspect_attempt_label",
            return_value=(None, None, True),
        ), mock.patch(
            "ossp_router.runtime._run_control_command",
        ) as control:
            error = _reconcile_resource_journal(("docker",), self.target)
        self.assertIn("미관찰 역할", error)
        self.assertTrue(_resource_journal_path(self.target).is_file())
        control.assert_not_called()

        removed = subprocess.CompletedProcess(("docker",), 0, b"", b"")
        with mock.patch(
            "ossp_router.runtime.inspect_runtime_daemon_id",
            return_value=TEST_DAEMON_ID,
        ), mock.patch(
            "ossp_router.runtime._inspect_attempt_label",
            side_effect=[
                (token, None, False),
                (None, None, True),
                (None, None, True),
                (None, None, True),
            ],
        ), mock.patch(
            "ossp_router.runtime._run_control_command",
            return_value=(removed, None),
        ) as control:
            error = _reconcile_resource_journal(("docker",), self.target)
        self.assertIn("helper", error)
        self.assertEqual(
            ["docker", "rm", "--force", journal.participant_name],
            control.call_args.args[0],
        )
        persisted = json.loads(
            _resource_journal_path(self.target).read_text(encoding="utf-8")
        )
        self.assertEqual(["participant"], persisted["observed_roles"])
        self.assertEqual(["docker"], persisted["runtime_command"])
        self.assertEqual(TEST_DAEMON_ID, persisted["daemon_id"])

        with mock.patch(
            "ossp_router.runtime.inspect_runtime_daemon_id",
            return_value=TEST_DAEMON_ID,
        ), mock.patch(
            "ossp_router.runtime._inspect_attempt_label",
            return_value=(None, None, True),
        ):
            error = _reconcile_resource_journal(
                ("docker",),
                self.target,
                confirm_daemon_restarted=True,
            )
        self.assertIsNone(error)
        self.assertFalse(_resource_journal_path(self.target).exists())

    def test_resource_journal_rejects_different_runtime_context(self) -> None:
        token = "c" * 32
        journal = _ResourceJournal(
            attempt_token=token,
            participant_name=f"ossp-router-participant-{token}",
            helper_name="ossp-router-helper-context",
            volume_name="ossp-router-output-context",
            runtime_command=("docker", "--context", "phase-c"),
            daemon_id=TEST_DAEMON_ID,
        )
        _write_resource_journal(self.target, journal)
        with mock.patch(
            "ossp_router.runtime._inspect_attempt_label",
        ) as inspect:
            error = _reconcile_resource_journal(
                ("docker", "--context", "different"),
                self.target,
            )
        self.assertIn("context", error)
        inspect.assert_not_called()
        self.assertTrue(_resource_journal_path(self.target).is_file())

    def test_resource_journal_rejects_changed_daemon_identity(self) -> None:
        token = "c" * 32
        _write_resource_journal(
            self.target,
            _ResourceJournal(
                attempt_token=token,
                participant_name=f"ossp-router-participant-{token}",
                helper_name="ossp-router-helper-daemon",
                volume_name="ossp-router-output-daemon",
                runtime_command=("docker", "--context", "phase-c"),
                daemon_id="original-engine",
            ),
        )
        with mock.patch(
            "ossp_router.runtime.inspect_runtime_daemon_id",
            return_value="replacement-engine",
        ), mock.patch(
            "ossp_router.runtime._inspect_attempt_label",
        ) as inspect:
            error = _reconcile_resource_journal(
                ("docker", "--context", "phase-c"),
                self.target,
                confirm_daemon_restarted=True,
            )
        self.assertIn("Engine ID", error)
        inspect.assert_not_called()
        self.assertTrue(_resource_journal_path(self.target).is_file())

    def test_sibling_pending_journal_blocks_before_runtime_mutation(self) -> None:
        official = self.target / "official"
        audit = self.target / "audit"
        official.mkdir(mode=0o700)
        audit.mkdir(mode=0o700)
        token = "c" * 32
        _write_resource_journal(
            audit,
            _ResourceJournal(
                attempt_token=token,
                participant_name=f"ossp-router-participant-{token}",
                helper_name="ossp-router-helper-audit-pending",
                volume_name="ossp-router-output-audit-pending",
                runtime_command=("docker",),
                daemon_id=TEST_DAEMON_ID,
            ),
        )
        with self.assertRaisesRegex(
            InfrastructureUnavailable,
            "이전 공식 또는 감사",
        ):
            require_no_pending_runtime_resources(
                ("docker",),
                (official, audit),
            )

    def test_resource_journal_never_removes_foreign_label(self) -> None:
        token = "c" * 32
        _write_resource_journal(
            self.target,
            _ResourceJournal(
                attempt_token=token,
                participant_name=f"ossp-router-participant-{token}",
                helper_name="ossp-router-helper-foreign",
                volume_name="ossp-router-output-foreign",
                runtime_command=("docker",),
                daemon_id=TEST_DAEMON_ID,
            ),
        )
        with mock.patch(
            "ossp_router.runtime.inspect_runtime_daemon_id",
            return_value=TEST_DAEMON_ID,
        ), mock.patch(
            "ossp_router.runtime._inspect_attempt_label",
            return_value=("d" * 32, None, False),
        ), mock.patch(
            "ossp_router.runtime._run_control_command",
        ) as control:
            error = _reconcile_resource_journal(("docker",), self.target)
        self.assertIn("충돌", error)
        control.assert_not_called()
        self.assertTrue(_resource_journal_path(self.target).is_file())

    def test_pending_journal_blocks_new_docker_mutation(self) -> None:
        token = "c" * 32
        _write_resource_journal(
            self.target,
            _ResourceJournal(
                attempt_token=token,
                participant_name=f"ossp-router-participant-{token}",
                helper_name="ossp-router-helper-pending",
                volume_name="ossp-router-output-pending",
                runtime_command=("docker",),
                daemon_id=TEST_DAEMON_ID,
            ),
        )
        output_directory = self.target
        with mock.patch(
            "ossp_router.runtime.inspect_runtime_daemon_id",
            return_value=TEST_DAEMON_ID,
        ), mock.patch(
            "ossp_router.runtime._inspect_attempt_label",
            return_value=(None, None, True),
        ), mock.patch(
            "ossp_router.runtime._create_output_volume",
        ) as create:
            result = run_isolated_container_once(
                ("docker",),
                image="sha256:" + "a" * 64,
                inputs=self.inputs,
                policy=self.policy,
                input_path=self.input_path,
                output_directory=output_directory,
                tier="fast",
            )
        self.assertEqual(AttemptKind.INFRASTRUCTURE_FAILURE, result.kind)
        self.assertTrue(result.cleanup_pending)
        self.assertEqual(token, result.cleanup_attempt_id)
        create.assert_not_called()

    def test_journal_write_failure_prevents_docker_mutation(self) -> None:
        with mock.patch(
            "ossp_router.runtime.validate_host_core_dump_policy",
            return_value=None,
        ), mock.patch(
            "ossp_router.runtime.validate_image_runtime_configuration",
            return_value=None,
        ), mock.patch(
            "ossp_router.runtime.inspect_runtime_daemon_id",
            return_value=TEST_DAEMON_ID,
        ), mock.patch(
            "ossp_router.runtime._write_resource_journal",
            side_effect=InfrastructureUnavailable("journal unavailable"),
        ), mock.patch(
            "ossp_router.runtime._create_output_volume",
        ) as create:
            result = run_isolated_container_once(
                ("docker",),
                image="sha256:" + "a" * 64,
                inputs=self.inputs,
                policy=self.policy,
                input_path=self.input_path,
                output_directory=self.target,
                tier="fast",
            )
        self.assertEqual(AttemptKind.INFRASTRUCTURE_FAILURE, result.kind)
        self.assertFalse(result.cleanup_pending)
        create.assert_not_called()

    def test_attempt_workspace_stages_helper_before_opening_sticky_dropbox(
        self,
    ) -> None:
        observed = {}
        real_copyfile = shutil.copyfile

        def checked_copy(source, destination):
            destination = pathlib.Path(destination)
            observed["mode_during_copy"] = (
                destination.parent.stat().st_mode & 0o7777
            )
            return real_copyfile(source, destination)

        def reject_helper_start(_runtime, **kwargs):
            directory = kwargs["result_directory"]
            helper = directory / "operator_helper.py"
            observed["mode_at_mount"] = directory.stat().st_mode & 0o7777
            observed["helper_mode"] = helper.stat().st_mode & 0o777
            observed["helper_uid"] = helper.stat().st_uid
            return "test stop before participant launch"

        with mock.patch(
            "ossp_router.runtime.validate_image_runtime_configuration",
            return_value=None,
        ), mock.patch(
            "ossp_router.runtime.inspect_runtime_daemon_id",
            return_value=TEST_DAEMON_ID,
        ), mock.patch(
            "ossp_router.runtime.shutil.copyfile",
            side_effect=checked_copy,
        ), mock.patch(
            "ossp_router.runtime._create_output_volume",
            return_value=None,
        ), mock.patch(
            "ossp_router.runtime._start_output_helper",
            side_effect=reject_helper_start,
        ), mock.patch(
            "ossp_router.runtime._remove_output_workspace",
            return_value=(),
        ):
            result = run_isolated_container_once(
                ("docker",),
                image="sha256:" + "a" * 64,
                inputs=self.inputs,
                policy=self.policy,
                input_path=self.input_path,
                output_directory=self.target,
                tier="fast",
            )

        self.assertEqual(AttemptKind.INFRASTRUCTURE_FAILURE, result.kind)
        self.assertEqual(0o700, observed["mode_during_copy"])
        self.assertEqual(0o1733, observed["mode_at_mount"])
        self.assertEqual(0o444, observed["helper_mode"])
        self.assertEqual(os.geteuid(), observed["helper_uid"])

    def test_operator_directory_rejects_nonsticky_writable_ancestor(
        self,
    ) -> None:
        shared = self.target / "shared-parent"
        shared.mkdir(mode=0o700)
        shared.chmod(0o777)
        output_directory = shared / "operator-output"
        output_directory.mkdir(mode=0o700)
        with self.assertRaisesRegex(
            InfrastructureUnavailable,
            "sticky bit",
        ):
            validate_operator_directory(
                output_directory,
                "test operator directory",
            )

    def test_operator_directory_allows_sticky_writable_ancestor(self) -> None:
        shared = self.target / "sticky-parent"
        shared.mkdir(mode=0o700)
        shared.chmod(0o1777)
        output_directory = shared / "operator-output"
        output_directory.mkdir(mode=0o700)
        self.assertEqual(
            output_directory.resolve(),
            validate_operator_directory(
                output_directory,
                "test operator directory",
            ),
        )

    def test_operator_directory_returns_canonical_symlink_target(self) -> None:
        canonical = self.target / "canonical-output"
        canonical.mkdir(mode=0o700)
        alias = self.target / "output-alias"
        alias.symlink_to(canonical, target_is_directory=True)
        self.assertEqual(
            canonical.resolve(),
            validate_operator_directory(alias, "test operator directory"),
        )

    def test_prepare_operator_directory_creates_below_canonical_alias(
        self,
    ) -> None:
        canonical_parent = self.target / "canonical-parent"
        canonical_parent.mkdir(mode=0o700)
        alias = self.target / "parent-alias"
        alias.symlink_to(canonical_parent, target_is_directory=True)
        prepared = prepare_operator_directory(
            alias / "nested" / "run",
            "test operator directory",
            parents=True,
        )
        self.assertEqual(
            (canonical_parent / "nested" / "run").resolve(),
            prepared,
        )
        self.assertEqual(0o700, prepared.stat().st_mode & 0o777)
        self.assertEqual(0o700, prepared.parent.stat().st_mode & 0o777)

    def test_isolated_runtime_rejects_nonprivate_output_directory(self) -> None:
        output_directory = self.target / "unsafe-output"
        output_directory.mkdir(mode=0o755)
        output_directory.chmod(0o755)
        with mock.patch(
            "ossp_router.runtime.validate_image_runtime_configuration"
        ) as preflight:
            result = run_isolated_container_once(
                ("docker",),
                image="sha256:" + "a" * 64,
                inputs=self.inputs,
                policy=self.policy,
                input_path=self.input_path,
                output_directory=output_directory,
                tier="fast",
            )
        self.assertEqual(AttemptKind.INFRASTRUCTURE_FAILURE, result.kind)
        self.assertIn("0700", result.detail)
        preflight.assert_not_called()

    def test_uncertain_workspace_creation_keeps_journal(self) -> None:
        with mock.patch(
            "ossp_router.runtime.validate_host_core_dump_policy",
            return_value=None,
        ), mock.patch(
            "ossp_router.runtime.validate_image_runtime_configuration",
            return_value=None,
        ), mock.patch(
            "ossp_router.runtime.inspect_runtime_daemon_id",
            return_value=TEST_DAEMON_ID,
        ), mock.patch(
            "ossp_router.runtime._create_output_volume",
            return_value="제어 명령 시간 초과",
        ), mock.patch(
            "ossp_router.runtime._remove_output_workspace",
            return_value=(),
        ):
            result = run_isolated_container_once(
                ("docker",),
                image="sha256:" + "a" * 64,
                inputs=self.inputs,
                policy=self.policy,
                input_path=self.input_path,
                output_directory=self.target,
                tier="fast",
            )
        self.assertEqual(AttemptKind.INFRASTRUCTURE_FAILURE, result.kind)
        self.assertTrue(result.cleanup_pending)
        self.assertRegex(result.cleanup_attempt_id or "", r"^[0-9a-f]{32}$")
        self.assertTrue(_resource_journal_path(self.target).is_file())

    def test_changed_daemon_before_cleanup_preserves_journal(self) -> None:
        with mock.patch(
            "ossp_router.runtime.validate_host_core_dump_policy",
            return_value=None,
        ), mock.patch(
            "ossp_router.runtime.validate_image_runtime_configuration",
            return_value=None,
        ), mock.patch(
            "ossp_router.runtime.inspect_runtime_daemon_id",
            side_effect=[
                "original-engine",
                "replacement-engine",
                "replacement-engine",
            ],
        ), mock.patch(
            "ossp_router.runtime._create_output_volume",
            return_value=None,
        ), mock.patch(
            "ossp_router.runtime._start_output_helper",
            return_value=None,
        ), mock.patch(
            "ossp_router.runtime.build_isolated_container_command",
            return_value=("fake-runtime",),
        ), mock.patch(
            "ossp_router.runtime._run_prepared_router_once",
            return_value=AttemptResult(
                AttemptKind.EXECUTION_FAILURE,
                "participant failed",
                returncode=1,
            ),
        ), mock.patch(
            "ossp_router.runtime._reconcile_and_remove_participant_container",
        ) as participant_cleanup, mock.patch(
            "ossp_router.runtime._remove_output_workspace",
        ) as workspace_cleanup:
            result = run_isolated_container_once(
                ("docker",),
                image="sha256:" + "a" * 64,
                inputs=self.inputs,
                policy=self.policy,
                input_path=self.input_path,
                output_directory=self.target,
                tier="fast",
            )
        self.assertEqual(AttemptKind.INFRASTRUCTURE_FAILURE, result.kind)
        self.assertTrue(result.cleanup_pending)
        participant_cleanup.assert_not_called()
        workspace_cleanup.assert_not_called()
        self.assertTrue(_resource_journal_path(self.target).is_file())

    def test_changed_daemon_before_journal_delete_preserves_journal(self) -> None:
        prepared = AttemptResult(
            AttemptKind.EXECUTION_FAILURE,
            "participant failed",
            returncode=1,
        )
        with mock.patch(
            "ossp_router.runtime.validate_host_core_dump_policy",
            return_value=None,
        ), mock.patch(
            "ossp_router.runtime.validate_image_runtime_configuration",
            return_value=None,
        ), mock.patch(
            "ossp_router.runtime.inspect_runtime_daemon_id",
            side_effect=[
                "original-engine",
                "original-engine",
                "original-engine",
                "replacement-engine",
            ],
        ), mock.patch(
            "ossp_router.runtime._create_output_volume",
            return_value=None,
        ), mock.patch(
            "ossp_router.runtime._start_output_helper",
            return_value=None,
        ), mock.patch(
            "ossp_router.runtime.build_isolated_container_command",
            return_value=("fake-runtime",),
        ), mock.patch(
            "ossp_router.runtime._run_prepared_router_once",
            return_value=prepared,
        ), mock.patch(
            "ossp_router.runtime._reconcile_and_remove_participant_container",
            return_value=prepared,
        ), mock.patch(
            "ossp_router.runtime._remove_output_workspace",
            return_value=(),
        ):
            result = run_isolated_container_once(
                ("docker",),
                image="sha256:" + "a" * 64,
                inputs=self.inputs,
                policy=self.policy,
                input_path=self.input_path,
                output_directory=self.target,
                tier="fast",
            )
        self.assertEqual(AttemptKind.INFRASTRUCTURE_FAILURE, result.kind)
        self.assertTrue(result.cleanup_pending)
        self.assertTrue(_resource_journal_path(self.target).is_file())

    def test_container_runtime_exit_125_without_cid_is_infrastructure(self) -> None:
        runtime_script = self.target / "fake-runtime-125.py"
        runtime_script.write_text(
            "import sys\n"
            "args=sys.argv[1:]\n"
            "if args[:2] == ['image','inspect']:\n"
            "    print('null\\nnull\\nnull\\nnull\\nfalse')\n"
            "elif args[:2] == ['volume','create']:\n"
            "    print(args[-1])\n"
            "elif args[0] == 'run' and '--detach' in args:\n"
            "    print('b'*64)\n"
            "elif args[0] == 'run':\n"
            "    raise SystemExit(125)\n"
            "elif args[0] == 'rm' or args[:2] == ['volume','rm']:\n"
            "    pass\n"
            "else:\n"
            "    raise SystemExit(2)\n",
            encoding="utf-8",
        )
        output_directory = self.target / "container-125-output"
        output_directory.mkdir(mode=0o700)
        stale_output = output_directory / "submission.json"
        stale_output.write_text('{"stale":true}', encoding="utf-8")
        with mock.patch(
            "ossp_router.runtime.validate_host_core_dump_policy",
            return_value=None,
        ), mock.patch(
            "ossp_router.runtime.inspect_runtime_daemon_id",
            return_value=TEST_DAEMON_ID,
        ), mock.patch(
            "ossp_router.runtime.secrets.token_hex",
            side_effect=lambda size: "d" * (size * 2),
        ), mock.patch(
            "ossp_router.runtime._inspect_attempt_label",
            return_value=("d" * 32, None, False),
        ):
            result = run_isolated_container_once(
                (sys.executable, str(runtime_script)),
                image="sha256:" + "c" * 64,
                inputs=self.inputs,
                policy=self.policy,
                input_path=self.input_path,
                output_directory=output_directory,
                tier="fast",
                timeout_seconds=1,
            )
        self.assertEqual(AttemptKind.INFRASTRUCTURE_FAILURE, result.kind)
        self.assertEqual(125, result.returncode)
        self.assertFalse(stale_output.exists())

    def test_docker_126_and_127_remain_participant_failures(self) -> None:
        token = "e" * 32
        snapshot = _ContainerStateSnapshot(
            container_id="a" * 64,
            attempt_token=token,
            status="created",
            exit_code=0,
            runtime_error=True,
            oom_killed=False,
            started_at="0001-01-01T00:00:00Z",
        )
        for returncode in (126, 127):
            with self.subTest(returncode=returncode), mock.patch(
                "ossp_router.runtime._inspect_participant_container",
                return_value=(snapshot, None, False),
            ), mock.patch(
                "ossp_router.runtime._remove_participant_container",
                return_value=None,
            ):
                result = _reconcile_and_remove_participant_container(
                    ("docker",),
                    container_name="ossp-router-participant-test",
                    attempt_token=token,
                    result=AttemptResult(
                        AttemptKind.EXECUTION_FAILURE,
                        f"비정상 종료 코드: {returncode}",
                        returncode=returncode,
                    ),
                    termination_grace_seconds=1,
                )
            self.assertEqual(AttemptKind.EXECUTION_FAILURE, result.kind)
            self.assertEqual(returncode, result.returncode)

    def test_never_started_125_is_infrastructure_failure(self) -> None:
        token = "e" * 32
        snapshot = _ContainerStateSnapshot(
            container_id="a" * 64,
            attempt_token=token,
            status="created",
            exit_code=0,
            runtime_error=True,
            oom_killed=False,
            started_at="0001-01-01T00:00:00Z",
        )
        with mock.patch(
            "ossp_router.runtime._inspect_participant_container",
            return_value=(snapshot, None, False),
        ), mock.patch(
            "ossp_router.runtime._remove_participant_container",
            return_value=None,
        ):
            result = _reconcile_and_remove_participant_container(
                ("docker",),
                container_name="ossp-router-participant-test",
                attempt_token=token,
                result=AttemptResult(
                    AttemptKind.EXECUTION_FAILURE,
                    "비정상 종료 코드: 125",
                    returncode=125,
                ),
                termination_grace_seconds=1,
            )
        self.assertEqual(AttemptKind.INFRASTRUCTURE_FAILURE, result.kind)

    def test_container_name_collision_never_removes_foreign_container(self) -> None:
        snapshot = _ContainerStateSnapshot(
            container_id="a" * 64,
            attempt_token="f" * 32,
            status="running",
            exit_code=0,
            runtime_error=False,
            oom_killed=False,
            started_at="2026-01-01T00:00:00Z",
        )
        with mock.patch(
            "ossp_router.runtime._inspect_participant_container",
            return_value=(snapshot, None, False),
        ), mock.patch(
            "ossp_router.runtime._remove_participant_container",
        ) as remove:
            result = _reconcile_and_remove_participant_container(
                ("docker",),
                container_name="ossp-router-participant-test",
                attempt_token="e" * 32,
                result=AttemptResult(
                    AttemptKind.EXECUTION_FAILURE,
                    "name collision",
                    returncode=125,
                ),
                termination_grace_seconds=1,
            )
        self.assertEqual(AttemptKind.INFRASTRUCTURE_FAILURE, result.kind)
        self.assertTrue(result.cleanup_pending)
        remove.assert_not_called()

    def test_participant_inspect_failure_requires_cleanup(self) -> None:
        with mock.patch(
            "ossp_router.runtime._inspect_participant_container",
            return_value=(None, "inspect failed", False),
        ):
            result = _reconcile_and_remove_participant_container(
                ("docker",),
                container_name="ossp-router-participant-test",
                attempt_token="e" * 32,
                result=AttemptResult(
                    AttemptKind.EXECUTION_FAILURE,
                    "runtime failed",
                    returncode=125,
                ),
                termination_grace_seconds=1,
            )
        self.assertEqual(AttemptKind.INFRASTRUCTURE_FAILURE, result.kind)
        self.assertTrue(result.cleanup_pending)

    def test_participant_remove_failure_requires_cleanup(self) -> None:
        token = "e" * 32
        snapshot = _ContainerStateSnapshot(
            container_id="a" * 64,
            attempt_token=token,
            status="exited",
            exit_code=1,
            runtime_error=False,
            oom_killed=False,
            started_at="2026-01-01T00:00:00Z",
        )
        observed = mock.Mock()
        with mock.patch(
            "ossp_router.runtime._inspect_participant_container",
            return_value=(snapshot, None, False),
        ), mock.patch(
            "ossp_router.runtime._remove_participant_container",
            return_value="remove failed",
        ):
            result = _reconcile_and_remove_participant_container(
                ("docker",),
                container_name="ossp-router-participant-test",
                attempt_token=token,
                result=AttemptResult(
                    AttemptKind.EXECUTION_FAILURE,
                    "participant failed",
                    returncode=1,
                ),
                termination_grace_seconds=1,
                observe_owned=observed,
            )
        observed.assert_called_once_with()
        self.assertEqual(AttemptKind.INFRASTRUCTURE_FAILURE, result.kind)
        self.assertTrue(result.cleanup_pending)

    def test_lock_contention_is_not_cleanup_pending(self) -> None:
        with mock.patch(
            "ossp_router.runtime._exclusive_resource_lock",
            side_effect=ResourceLockUnavailable("already running"),
        ):
            result = run_isolated_container_once(
                ("docker",),
                image="sha256:" + "a" * 64,
                inputs=self.inputs,
                policy=self.policy,
                input_path=self.input_path,
                output_directory=self.target,
                tier="fast",
            )
        self.assertEqual(AttemptKind.INFRASTRUCTURE_FAILURE, result.kind)
        self.assertFalse(result.cleanup_pending)

    def test_started_participant_exit_125_is_execution_failure(self) -> None:
        runtime_script = self.target / "fake-participant-125.py"
        state_path = self.target / "participant-125-state.json"
        runtime_script.write_text(
            "import json,pathlib,sys\n"
            "args=sys.argv[1:]\n"
            f"state_path=pathlib.Path({str(state_path)!r})\n"
            "if args[:2] == ['image','inspect']:\n"
            "    print('null\\nnull\\nnull\\nnull\\nfalse')\n"
            "elif args[:2] == ['volume','create']:\n"
            "    print(args[-1])\n"
            "elif args[0] == 'run' and '--detach' in args:\n"
            "    print('b'*64)\n"
            "elif args[0] == 'run':\n"
            "    cidfile=args[args.index('--cidfile')+1]\n"
            "    pathlib.Path(cidfile).write_text('a'*64,encoding='ascii')\n"
            "    token=args[args.index('--label')+1].split('=',1)[1]\n"
            "    state={'token':token,'Status':'exited','ExitCode':125,"
            "'Error':'','OOMKilled':False,"
            "'StartedAt':'2026-01-01T00:00:00Z'}\n"
            "    state_path.write_text(json.dumps(state),encoding='utf-8')\n"
            "    raise SystemExit(125)\n"
            "elif args[:2] == ['container','inspect']:\n"
            "    state=json.loads(state_path.read_text(encoding='utf-8'))\n"
            "    [print(json.dumps(bool(state[key]) if key=='Error' else state[key])) for key in "
            "('Status','ExitCode','Error','OOMKilled','StartedAt')]\n"
            "    print(json.dumps(state['token']))\n"
            "    print(json.dumps('a'*64))\n"
            "elif args[0] == 'rm':\n"
            "    state_path.unlink(missing_ok=True)\n"
            "elif args[:2] == ['volume','rm']:\n"
            "    pass\n"
            "else:\n"
            "    raise SystemExit(2)\n",
            encoding="utf-8",
        )
        output_directory = self.target / "participant-125-output"
        output_directory.mkdir(mode=0o700)
        with mock.patch(
            "ossp_router.runtime.validate_host_core_dump_policy",
            return_value=None,
        ), mock.patch(
            "ossp_router.runtime.inspect_runtime_daemon_id",
            return_value=TEST_DAEMON_ID,
        ), mock.patch(
            "ossp_router.runtime.secrets.token_hex",
            side_effect=lambda size: "d" * (size * 2),
        ), mock.patch(
            "ossp_router.runtime._inspect_attempt_label",
            return_value=("d" * 32, None, False),
        ):
            result = run_isolated_container_once(
                (sys.executable, str(runtime_script)),
                image="sha256:" + "c" * 64,
                inputs=self.inputs,
                policy=self.policy,
                input_path=self.input_path,
                output_directory=output_directory,
                tier="fast",
                timeout_seconds=1,
            )
        self.assertEqual(AttemptKind.EXECUTION_FAILURE, result.kind)
        self.assertEqual(125, result.returncode)

    def test_unexpected_runner_error_still_cleans_helper_and_volume(self) -> None:
        output_directory = self.target / "unexpected-error-output"
        output_directory.mkdir(mode=0o700)
        with mock.patch(
            "ossp_router.runtime.validate_host_core_dump_policy",
            return_value=None,
        ), mock.patch(
            "ossp_router.runtime.validate_image_runtime_configuration",
            return_value=None,
        ), mock.patch(
            "ossp_router.runtime.inspect_runtime_daemon_id",
            return_value=TEST_DAEMON_ID,
        ), mock.patch(
            "ossp_router.runtime._create_output_volume",
            return_value=None,
        ), mock.patch(
            "ossp_router.runtime._start_output_helper",
            return_value=None,
        ), mock.patch(
            "ossp_router.runtime.build_isolated_container_command",
            return_value=("fake-runtime",),
        ), mock.patch(
            "ossp_router.runtime._run_prepared_router_once",
            side_effect=RuntimeError("unexpected"),
        ), mock.patch(
            "ossp_router.runtime._remove_output_workspace",
            return_value=(),
        ) as cleanup, mock.patch(
            "ossp_router.runtime._best_effort_remove_participant_container",
        ) as participant_cleanup:
            with self.assertRaisesRegex(RuntimeError, "unexpected"):
                run_isolated_container_once(
                    ("docker",),
                    image="sha256:" + "c" * 64,
                    inputs=self.inputs,
                    policy=self.policy,
                    input_path=self.input_path,
                    output_directory=output_directory,
                    tier="fast",
                )
        cleanup.assert_called_once()
        participant_cleanup.assert_called_once()
        self.assertTrue(cleanup.call_args.kwargs["helper_started"])
        self.assertTrue(cleanup.call_args.kwargs["volume_created"])

    def test_direct_process_exit_125_is_execution_failure(self) -> None:
        result = self._run(
            [sys.executable, "-c", "raise SystemExit(125)"],
        )
        self.assertEqual(AttemptKind.EXECUTION_FAILURE, result.kind)
        self.assertEqual(125, result.returncode)

    def test_timeout_is_stopped_by_unique_name_without_cidfile(self) -> None:
        runtime_script = self.target / "fake-container-runtime.py"
        cleanup_marker = self.target / "cleanup.txt"
        state_path = self.target / "timeout-state.json"
        runtime_script.write_text(
            "import json,pathlib,sys,time\n"
            "args=sys.argv[1:]\n"
            f"state_path=pathlib.Path({str(state_path)!r})\n"
            "if args[:2] == ['image','inspect']:\n"
            "    print('null\\nnull\\nnull\\nnull\\nfalse')\n"
            "elif args[:2] == ['volume','create']:\n"
            "    print(args[-1])\n"
            "elif args[0] == 'run' and '--detach' in args:\n"
            "    print('b'*64)\n"
            "elif args[0] == 'run':\n"
            "    token=args[args.index('--label')+1].split('=',1)[1]\n"
            "    state={'token':token,'Status':'running','ExitCode':0,"
            "'Error':'','OOMKilled':False,"
            "'StartedAt':'2026-01-01T00:00:00Z'}\n"
            "    state_path.write_text(json.dumps(state),encoding='utf-8')\n"
            "    time.sleep(10)\n"
            "elif args[:2] == ['container','inspect']:\n"
            "    state=json.loads(state_path.read_text(encoding='utf-8'))\n"
            "    [print(json.dumps(bool(state[key]) if key=='Error' else state[key])) for key in "
            "('Status','ExitCode','Error','OOMKilled','StartedAt')]\n"
            "    print(json.dumps(state['token']))\n"
            "    print(json.dumps('a'*64))\n"
            "elif args[0] == 'stop':\n"
            f"    pathlib.Path({str(cleanup_marker)!r}).write_text("
            "' '.join(args),encoding='utf-8')\n"
            "    state=json.loads(state_path.read_text(encoding='utf-8'))\n"
            "    state.update(Status='exited',ExitCode=137)\n"
            "    state_path.write_text(json.dumps(state),encoding='utf-8')\n"
            "elif args[0] == 'rm':\n"
            "    state_path.unlink(missing_ok=True)\n"
            "elif args[:2] == ['volume','rm']:\n"
            "    pass\n"
            "else:\n"
            "    raise SystemExit(2)\n",
            encoding="utf-8",
        )
        output_directory = self.target / "container-timeout-output"
        output_directory.mkdir(mode=0o700)
        with mock.patch(
            "ossp_router.runtime.validate_host_core_dump_policy",
            return_value=None,
        ), mock.patch(
            "ossp_router.runtime.inspect_runtime_daemon_id",
            return_value=TEST_DAEMON_ID,
        ), mock.patch(
            "ossp_router.runtime.secrets.token_hex",
            side_effect=lambda size: "d" * (size * 2),
        ), mock.patch(
            "ossp_router.runtime._inspect_attempt_label",
            return_value=("d" * 32, None, False),
        ):
            result = run_isolated_container_once(
                (sys.executable, str(runtime_script)),
                image="sha256:" + "c" * 64,
                inputs=self.inputs,
                policy=self.policy,
                input_path=self.input_path,
                output_directory=output_directory,
                tier="fast",
                timeout_seconds=0.2,
                termination_grace_seconds=0.1,
            )
        self.assertEqual(AttemptKind.EXECUTION_FAILURE, result.kind)
        self.assertTrue(result.timed_out)
        self.assertIn("stop --timeout 0", cleanup_marker.read_text(encoding="utf-8"))


@unittest.skipUnless(
    _docker_integration_enabled(),
    "OSSP_RUN_CONTAINER_TESTS=1 및 실행 중인 Docker가 필요합니다.",
)
class ContainerIsolationIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tag_one = f"ossp-router-phase-c:{os.getpid()}-one"
        cls.tag_two = f"ossp-router-phase-c:{os.getpid()}-two"
        cls.tag_extra_output = f"ossp-router-phase-c:{os.getpid()}-extra"
        cls.tag_declared_volume = f"ossp-router-phase-c:{os.getpid()}-volume"
        cls.tag_chmod_output = f"ossp-router-phase-c:{os.getpid()}-chmod"
        for tag in (cls.tag_one, cls.tag_two):
            built = subprocess.run(
                [
                    "docker",
                    "build",
                    "--pull=false",
                    "--no-cache",
                    "--tag",
                    tag,
                    "--file",
                    str(DOCKERFILE),
                    str(ROOT),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if built.returncode != 0:
                raise AssertionError(built.stderr)
        inspected = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", cls.tag_one],
            capture_output=True,
            text=True,
            check=False,
        )
        if inspected.returncode != 0:
            raise AssertionError(inspected.stderr)
        cls.image_one = inspected.stdout.strip()
        malicious_script = (
            "from ossp_router.heuristic import main\n"
            "import pathlib,sys\n"
            "result=main()\n"
            "arguments=sys.argv[1:]\n"
            "output=pathlib.Path(arguments[arguments.index('--output')+1])\n"
            "tier=arguments[arguments.index('--tier')+1]\n"
            "size=1 if tier=='fast' else 8*1024*1024\n"
            "try:\n"
            "    output.with_name('extra.bin').write_bytes(b'x'*size)\n"
            "except OSError:\n"
            "    pass\n"
            "raise SystemExit(result)\n"
        )
        dockerfile = (
            f"FROM {cls.tag_one}\n"
            f"ENTRYPOINT {json.dumps(['python3', '-c', malicious_script])}\n"
        )
        built = subprocess.run(
            ["docker", "build", "--pull=false", "--tag", cls.tag_extra_output, "-"],
            input=dockerfile,
            capture_output=True,
            text=True,
            check=False,
        )
        if built.returncode != 0:
            raise AssertionError(built.stderr)
        inspected = subprocess.run(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                "{{.Id}}",
                cls.tag_extra_output,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if inspected.returncode != 0:
            raise AssertionError(inspected.stderr)
        cls.extra_output_image = inspected.stdout.strip()
        declared_volume_script = (
            "from ossp_router.heuristic import main;"
            "import pathlib;"
            "pathlib.Path('/challenge/output/hidden/extra.bin').write_bytes("
            "b'x'*(8*1024*1024));"
            "raise SystemExit(main())"
        )
        declared_volume_dockerfile = (
            f"FROM {cls.tag_one}\n"
            'VOLUME ["/challenge/output/hidden"]\n'
            f"ENTRYPOINT {json.dumps(['python3', '-c', declared_volume_script])}\n"
        )
        built = subprocess.run(
            [
                "docker",
                "build",
                "--pull=false",
                "--tag",
                cls.tag_declared_volume,
                "-",
            ],
            input=declared_volume_dockerfile,
            capture_output=True,
            text=True,
            check=False,
        )
        if built.returncode != 0:
            raise AssertionError(built.stderr)
        inspected = subprocess.run(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                "{{.Id}}",
                cls.tag_declared_volume,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if inspected.returncode != 0:
            raise AssertionError(inspected.stderr)
        cls.declared_volume_image = inspected.stdout.strip()
        chmod_output_script = (
            "from ossp_router.heuristic import main\n"
            "import pathlib,sys\n"
            "result=main()\n"
            "arguments=sys.argv[1:]\n"
            "output=pathlib.Path(arguments[arguments.index('--output')+1])\n"
            "output.parent.chmod(0)\n"
            "raise SystemExit(result)\n"
        )
        chmod_output_dockerfile = (
            f"FROM {cls.tag_one}\n"
            f"ENTRYPOINT {json.dumps(['python3', '-c', chmod_output_script])}\n"
        )
        built = subprocess.run(
            [
                "docker",
                "build",
                "--pull=false",
                "--tag",
                cls.tag_chmod_output,
                "-",
            ],
            input=chmod_output_dockerfile,
            capture_output=True,
            text=True,
            check=False,
        )
        if built.returncode != 0:
            raise AssertionError(built.stderr)
        inspected = subprocess.run(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                "{{.Id}}",
                cls.tag_chmod_output,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if inspected.returncode != 0:
            raise AssertionError(inspected.stderr)
        cls.chmod_output_image = inspected.stdout.strip()

    @classmethod
    def tearDownClass(cls) -> None:
        subprocess.run(
            [
                "docker",
                "image",
                "rm",
                "--force",
                cls.tag_one,
                cls.tag_two,
                cls.tag_extra_output,
                cls.tag_declared_volume,
                cls.tag_chmod_output,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )

    def _inspect(self, tag):
        inspected = subprocess.run(
            ["docker", "image", "inspect", tag],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, inspected.returncode, inspected.stderr)
        return json.loads(inspected.stdout)[0]

    def test_dockerignore_excludes_unlisted_context_files(self) -> None:
        tag = f"ossp-router-phase-c:{os.getpid()}-context-probe"
        with tempfile.TemporaryDirectory() as temporary:
            context = pathlib.Path(temporary)
            shutil.copyfile(ROOT / ".dockerignore", context / ".dockerignore")
            source = context / "src/ossp_router"
            source.mkdir(parents=True)
            (source / "__init__.py").write_text("", encoding="utf-8")
            (context / "src/secret.key").write_text("sentinel", encoding="utf-8")
            (source / ".env").write_text("sentinel", encoding="utf-8")
            (source / "cache.pyc").write_bytes(b"sentinel")
            (source / "unlisted.txt").write_text("sentinel", encoding="utf-8")

            container = context / "container"
            container.mkdir()
            (container / "entrypoint.py").write_text("", encoding="utf-8")
            (container / "README.md").write_text("sentinel", encoding="utf-8")
            (container / ".env").write_text("sentinel", encoding="utf-8")
            dockerfile = "\n".join(
                (
                    f"FROM {self.image_one}",
                    "COPY src /probe/src",
                    "COPY container /probe/container",
                    "RUN test -f /probe/src/ossp_router/__init__.py "
                    "&& test -f /probe/container/entrypoint.py "
                    "&& test ! -e /probe/src/secret.key "
                    "&& test ! -e /probe/src/ossp_router/.env "
                    "&& test ! -e /probe/src/ossp_router/cache.pyc "
                    "&& test ! -e /probe/src/ossp_router/unlisted.txt "
                    "&& test ! -e /probe/container/README.md "
                    "&& test ! -e /probe/container/.env",
                    "",
                )
            )
            (container / "Dockerfile").write_text(dockerfile, encoding="utf-8")
            try:
                built = subprocess.run(
                    [
                        "docker",
                        "build",
                        "--pull=false",
                        "--no-cache",
                        "--tag",
                        tag,
                        "--file",
                        str(container / "Dockerfile"),
                        str(context),
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(0, built.returncode, built.stderr)
            finally:
                subprocess.run(
                    ["docker", "image", "rm", "--force", tag],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )

    def test_cached_repository_digest_resolution_uses_linux_arm64(
        self,
    ) -> None:
        metadata = inspect_image_runtime_metadata(
            ("docker",),
            BASE_REPOSITORY_DIGEST,
            platform="linux/arm64",
        )
        evidence = ImageSizeEvidence(
            submitted_digest=BASE_REPOSITORY_DIGEST,
            selected_manifest_digest=metadata["Id"],
            config_digest=metadata["Id"],
            operating_system=metadata["Os"],
            architecture=metadata["Architecture"],
            oci_compressed_layer_bytes=1,
            rootfs_apparent_bytes=1,
            oci_layer_measurement_method=(
                "oci-manifest-layer-descriptors-v1"
            ),
            rootfs_measurement_method=(
                "docker-export-tar-apparent-size-v1"
            ),
            evidence_sha256="e" * 64,
        )
        resolved = resolve_submitted_image(
            ("docker",),
            BASE_REPOSITORY_DIGEST,
            evidence,
        )
        self.assertTrue(resolved.local_image_id.startswith("sha256:"))
        self.assertEqual("linux", resolved.operating_system)
        self.assertEqual("arm64", resolved.architecture)
        self.assertEqual(
            BASE_REPOSITORY_DIGEST,
            resolved.execution_image_reference,
        )

    def test_rootfs_measurement_runs_named_digest_with_explicit_platform(
        self,
    ) -> None:
        if self._inspect(BASE_REPOSITORY_DIGEST)["Architecture"] != "arm64":
            self.skipTest("official linux/arm64 image layers are not cached")
        with tempfile.TemporaryDirectory(
            prefix=".ossp-router-image-measurement-",
            dir=pathlib.Path.home(),
        ) as temporary:
            target = pathlib.Path(temporary)
            target.chmod(0o700)
            journal = target / ".measurement-journal.json"
            apparent = _measure_export_tar(
                ("docker",),
                BASE_REPOSITORY_DIGEST,
                "linux/arm64",
                journal,
            )
            self.assertGreater(apparent, 0)
            self.assertFalse(journal.exists())

    def test_nested_image_declared_volume_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "VOLUME"):
            validate_image_runtime_configuration(
                ("docker",),
                self.tag_declared_volume,
            )
        with tempfile.TemporaryDirectory(
            prefix=".ossp-router-volume-test-",
            dir=pathlib.Path.home(),
        ) as temporary:
            target = pathlib.Path(temporary)
            input_path = target / "inputs.json"
            output_directory = target / "output"
            shutil.copyfile(ROOT / "data/toy/inputs.json", input_path)
            output_directory.mkdir(mode=0o700)
            with self.assertRaisesRegex(ValueError, "VOLUME"):
                run_isolated_container_once(
                    ("docker",),
                    image=self.declared_volume_image,
                    inputs=load_input(input_path),
                    policy=load_bundled_policy(),
                    input_path=input_path,
                    output_directory=output_directory,
                    tier="fast",
                    timeout_seconds=10,
                )

    def _runtime_content_fingerprint(self, tag):
        script = (
            "import hashlib,pathlib;"
            "root=pathlib.Path('/opt/router');"
            "digest=hashlib.sha256();"
            "items=sorted(root.rglob('*'),key=lambda p:p.as_posix());"
            "[(digest.update(p.relative_to(root).as_posix().encode()),"
            "digest.update(str(p.stat().st_mode & 0o777).encode()),"
            "digest.update(p.read_bytes()) if p.is_file() else None) for p in items];"
            "print(digest.hexdigest())"
        )
        completed = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--entrypoint",
                "python3",
                tag,
                "-c",
                script,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        return completed.stdout.strip()

    def test_two_clean_builds_have_same_runtime_content_and_config(self) -> None:
        one = self._inspect(self.tag_one)
        two = self._inspect(self.tag_two)
        self.assertEqual(
            self._runtime_content_fingerprint(self.tag_one),
            self._runtime_content_fingerprint(self.tag_two),
        )
        self.assertEqual(one["Architecture"], two["Architecture"])
        self.assertEqual(one["Os"], two["Os"])
        for field in ("Entrypoint", "Env", "User", "WorkingDir"):
            self.assertEqual(one["Config"][field], two["Config"][field])

    def test_operator_container_runner_returns_valid_submission(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix=".ossp-router-operator-test-",
            dir=pathlib.Path.home(),
        ) as temporary:
            target = pathlib.Path(temporary)
            input_path = target / "inputs.json"
            output_directory = target / "output"
            shutil.copyfile(ROOT / "data/toy/inputs.json", input_path)
            output_directory.mkdir(mode=0o700)
            inputs = load_input(input_path)
            policy = load_bundled_policy()
            result = run_isolated_container_once(
                ("docker",),
                image=self.image_one,
                inputs=inputs,
                policy=policy,
                input_path=input_path,
                output_directory=output_directory,
                tier="fast",
                timeout_seconds=10,
            )
            self.assertEqual(AttemptKind.VALID, result.kind, result.detail)
            self.assertIsNotNone(result.submission)
            self.assertFalse(
                (output_directory / RESOURCE_JOURNAL_NAME).exists()
            )

    def test_official_orchestrator_mounts_private_snapshot_for_nonroot(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix=".ossp-router-official-test-",
            dir=pathlib.Path.home(),
        ) as temporary:
            target = pathlib.Path(temporary)
            input_path = target / "inputs.json"
            output_directory = target / "output"
            private_work_directory = target / "private"
            shutil.copyfile(ROOT / "data/toy/inputs.json", input_path)
            private_work_directory.mkdir(mode=0o700)
            inputs = load_input(input_path)
            policy = load_bundled_policy()

            def resolver(_runtime, submitted, _evidence):
                return ResolvedImage(
                    submitted,
                    self.image_one,
                    "linux",
                    "arm64",
                    "sha256:" + "f" * 64,
                    1024,
                    2048,
                    "e" * 64,
                )

            record_path = output_directory / "execution-record.json"
            outcome = run_official_tier(
                runtime_command=("docker",),
                submitted_image_digest=(
                    "registry.example/router@sha256:" + "a" * 64
                ),
                commit_sha="b" * 40,
                inputs=inputs,
                policy=policy,
                input_path=input_path,
                output_directory=output_directory,
                private_work_directory=private_work_directory,
                tier="fast",
                audit_secret=b"official-docker-audit",
                image_resolver=resolver,
                record_path=record_path,
            )

            self.assertEqual(AuditStatus.PASSED, outcome.audit.status)
            record = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual("valid", record["status"])
            self.assertEqual([], list(private_work_directory.iterdir()))
            self.assertFalse((output_directory / "audit" / "inputs.json").exists())

    def test_participant_output_directory_permission_change_is_failure(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix=".ossp-router-chmod-output-test-",
            dir=pathlib.Path.home(),
        ) as temporary:
            target = pathlib.Path(temporary)
            input_path = target / "inputs.json"
            output_directory = target / "output"
            shutil.copyfile(ROOT / "data/toy/inputs.json", input_path)
            output_directory.mkdir(mode=0o700)
            result = run_isolated_container_once(
                ("docker",),
                image=self.chmod_output_image,
                inputs=load_input(input_path),
                policy=load_bundled_policy(),
                input_path=input_path,
                output_directory=output_directory,
                tier="fast",
                timeout_seconds=10,
            )
            self.assertEqual(
                AttemptKind.EXECUTION_FAILURE,
                result.kind,
                result.detail,
            )
            self.assertIn("출력 공간 검증 실패", result.detail)
            self.assertFalse(
                (output_directory / "submission.json").exists()
            )

    def test_networkless_read_only_nonroot_execution(self) -> None:
        probe = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
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
                "--entrypoint",
                "python3",
                self.tag_one,
                "-c",
                (
                    "import glob,resource,socket;"
                    "assert all(name == 'lo' for _,name in socket.if_nameindex());"
                    "assert not glob.glob('/dev/nvidia*');"
                    "assert resource.getrlimit(resource.RLIMIT_CORE) == (0,0)"
                ),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, probe.returncode, probe.stderr)

        # Colima cannot bind-mount a macOS privacy-protected Documents folder
        # unless the host process has separate Files and Folders permission.
        # Stage public toy input directly under the user's shared home instead.
        with tempfile.TemporaryDirectory(
            prefix=".ossp-router-container-test-",
            dir=pathlib.Path.home(),
        ) as temporary:
            target = pathlib.Path(temporary)
            input_dir = target / "input"
            output_dir = target / "output"
            shutil.copytree(ROOT / "data/toy", input_dir)
            output_dir.mkdir(mode=0o700)
            inputs = load_input(input_dir / "inputs.json")
            result = run_isolated_container_once(
                ("docker",),
                image=self.image_one,
                inputs=inputs,
                policy=load_bundled_policy(),
                input_path=input_dir / "inputs.json",
                output_directory=output_dir,
                tier="fast",
                timeout_seconds=10,
            )
            self.assertEqual(AttemptKind.VALID, result.kind, result.detail)
            submission = json.loads(
                (output_dir / "submission.json").read_text(encoding="utf-8")
            )
            self.assertEqual("fast", submission["tier"])

    def test_auxiliary_output_files_are_rejected_and_cleaned(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix=".ossp-router-extra-output-test-",
            dir=pathlib.Path.home(),
        ) as temporary:
            target = pathlib.Path(temporary)
            input_path = target / "inputs.json"
            shutil.copyfile(ROOT / "data/toy/inputs.json", input_path)
            inputs = load_input(input_path)
            policy = load_bundled_policy()
            for tier in ("fast", "balanced"):
                output_directory = target / tier
                output_directory.mkdir(mode=0o700)
                result = run_isolated_container_once(
                    ("docker",),
                    image=self.extra_output_image,
                    inputs=inputs,
                    policy=policy,
                    input_path=input_path,
                    output_directory=output_directory,
                    tier=tier,
                    timeout_seconds=10,
                )
                self.assertEqual(
                    AttemptKind.EXECUTION_FAILURE,
                    result.kind,
                    result.detail,
                )
                self.assertIn("출력 공간 검증 실패", result.detail)
                self.assertFalse(
                    (output_directory / "submission.json").exists()
                )

        volumes = subprocess.run(
            [
                "docker",
                "volume",
                "ls",
                "--filter",
                "name=ossp-router-output-",
                "--format",
                "{{.Name}}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, volumes.returncode, volumes.stderr)
        self.assertEqual("", volumes.stdout.strip())
        helpers = subprocess.run(
            [
                "docker",
                "ps",
                "--all",
                "--filter",
                "name=ossp-router-helper-",
                "--format",
                "{{.Names}}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, helpers.returncode, helpers.stderr)
        self.assertEqual("", helpers.stdout.strip())
        participants = subprocess.run(
            [
                "docker",
                "ps",
                "--all",
                "--filter",
                "name=ossp-router-participant-",
                "--format",
                "{{.Names}}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, participants.returncode, participants.stderr)
        self.assertEqual("", participants.stdout.strip())


if __name__ == "__main__":
    unittest.main()
