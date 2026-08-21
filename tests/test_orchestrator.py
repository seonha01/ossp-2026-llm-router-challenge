# SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import json
import pathlib
import tempfile
import unittest
from types import SimpleNamespace
from typing import Optional
from unittest import mock

from ossp_router.orchestrator import (
    AuditStatus,
    ResolvedImage,
    _attempt_to_dict,
    _record_matches_cleanup_attempt,
    _write_record_atomic,
    main,
    make_id_order_audit_variant,
    official_tier_outcome_to_dict,
    resolve_submitted_image,
    run_id_order_audit,
    run_official_tier,
)
from ossp_router.protocol import (
    Decision,
    Episode,
    InputBatch,
    Submission,
    load_bundled_policy,
    load_input,
    write_json,
)
from ossp_router.runtime import (
    AttemptKind,
    AttemptResult,
    DeclaredVolumeNotAllowed,
    ImageLimitExceeded,
    ImageSizeEvidence,
    InfrastructureUnavailable,
    PHASE_C_CANDIDATE_LIMITS,
    PendingRuntimeResources,
    ResourceLockUnavailable,
    _ResourceJournal,
    _write_resource_journal,
    exclusive_evaluation_lock,
)


NAMED_DIGEST = "registry.example/challenge/router@sha256:" + "a" * 64
LOCAL_IMAGE_ID = "sha256:" + "b" * 64
COMMIT_SHA = "c" * 40
SELECTED_MANIFEST_DIGEST = "sha256:" + "d" * 64


def _size_evidence(
    *,
    compressed: int = 1000,
    rootfs: int = 2000,
) -> ImageSizeEvidence:
    return ImageSizeEvidence(
        submitted_digest=NAMED_DIGEST,
        selected_manifest_digest=SELECTED_MANIFEST_DIGEST,
        config_digest=LOCAL_IMAGE_ID,
        operating_system="linux",
        architecture="arm64",
        oci_compressed_layer_bytes=compressed,
        rootfs_apparent_bytes=rootfs,
        oci_layer_measurement_method="oci-manifest-layer-descriptors-v1",
        rootfs_measurement_method="docker-export-tar-apparent-size-v1",
        evidence_sha256="f" * 64,
    )


def _resolved_image(
    submitted: str = NAMED_DIGEST,
    *,
    execution_image_reference: Optional[str] = None,
) -> ResolvedImage:
    evidence = _size_evidence()
    return ResolvedImage(
        submitted,
        LOCAL_IMAGE_ID,
        "linux",
        "arm64",
        evidence.selected_manifest_digest,
        evidence.oci_compressed_layer_bytes,
        evidence.rootfs_apparent_bytes,
        evidence.evidence_sha256,
        execution_image_reference,
    )


def _inputs() -> InputBatch:
    return InputBatch(
        schema_version=1,
        challenge_id="phase-c-orchestrator",
        split="synthetic",
        episodes=(
            Episode("original-one", prompt="same prompt"),
            Episode("original-two", prompt="same prompt"),
            Episode("original-three", prompt="different prompt"),
        ),
    )


def _submission(inputs: InputBatch, tier: str = "fast") -> Submission:
    return Submission(
        schema_version=inputs.schema_version,
        challenge_id=inputs.challenge_id,
        policy_id=load_bundled_policy().policy_id,
        split=inputs.split,
        tier=tier,
        decisions=tuple(
            Decision(episode.episode_id, "ax31-light")
            for episode in inputs.episodes
        ),
    )


def _private_work_directory(target: pathlib.Path) -> pathlib.Path:
    path = target / "private-work"
    path.mkdir()
    path.chmod(0o700)
    return path


def _image_size_evidence_path(target: pathlib.Path) -> pathlib.Path:
    directory = target / "image-evidence"
    directory.mkdir(mode=0o700)
    path = directory / "linux-arm64.json"
    evidence = _size_evidence()
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "report_type": "operator-image-size-evidence",
                "submitted_digest": evidence.submitted_digest,
                "selected_manifest_digest": (
                    evidence.selected_manifest_digest
                ),
                "config_digest": evidence.config_digest,
                "os": evidence.operating_system,
                "architecture": evidence.architecture,
                "oci_compressed_layer_bytes": (
                    evidence.oci_compressed_layer_bytes
                ),
                "rootfs_apparent_bytes": evidence.rootfs_apparent_bytes,
                "oci_layer_measurement_method": (
                    evidence.oci_layer_measurement_method
                ),
                "rootfs_measurement_method": (
                    evidence.rootfs_measurement_method
                ),
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def _cli_outcome(
    *,
    audit_status: Optional[AuditStatus],
    infrastructure_unavailable: bool = False,
    tier_score_zero: bool = False,
    disqualified: bool = False,
):
    audit = (
        None
        if audit_status is None
        else SimpleNamespace(status=audit_status)
    )
    return SimpleNamespace(
        retry=SimpleNamespace(
            infrastructure_unavailable=infrastructure_unavailable,
            tier_score_zero=tier_score_zero,
            disqualified=disqualified,
        ),
        audit=audit,
    )


class OrchestratorTest(unittest.TestCase):
    def test_cleanup_record_recursion_is_treated_as_nonmatching(self) -> None:
        deeply_nested = "[" * 2000 + "0" + "]" * 2000
        with tempfile.TemporaryDirectory() as temporary:
            path = pathlib.Path(temporary) / "record.json"
            path.write_text(deeply_nested, encoding="utf-8")
            self.assertFalse(
                _record_matches_cleanup_attempt(path, "a" * 32)
            )

    def test_resolve_submitted_image_rejects_mutable_tag(self) -> None:
        invalid = (
            "router:latest",
            "registry.example/router@sha256:abc",
            "registry.example/router@sha256:" + "A" * 64,
            "registry.example/router:tag@sha256:" + "a" * 64,
        )
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "다이제스트"):
                    resolve_submitted_image(("docker",), value)

    @mock.patch("ossp_router.orchestrator.inspect_image_runtime_metadata")
    def test_resolve_submitted_image_records_local_id_and_platform(
        self,
        run: mock.Mock,
    ) -> None:
        run.return_value = {
            "Id": LOCAL_IMAGE_ID,
            "RepoDigests": [NAMED_DIGEST],
            "Os": "linux",
            "Architecture": "arm64",
            "Config": {"Volumes": None},
        }
        resolved = resolve_submitted_image(
            ("docker",),
            NAMED_DIGEST,
            _size_evidence(),
        )
        self.assertEqual(LOCAL_IMAGE_ID, resolved.local_image_id)
        self.assertEqual("linux", resolved.operating_system)
        self.assertEqual("arm64", resolved.architecture)
        self.assertEqual(
            SELECTED_MANIFEST_DIGEST,
            resolved.selected_manifest_digest,
        )
        self.assertEqual(1000, resolved.oci_compressed_layer_bytes)
        self.assertEqual(2000, resolved.rootfs_apparent_bytes)
        self.assertEqual(NAMED_DIGEST, resolved.execution_image_reference)
        run.assert_called_once_with(
            ("docker",),
            NAMED_DIGEST,
            platform="linux/arm64",
        )

    @mock.patch("ossp_router.orchestrator.inspect_image_runtime_metadata")
    def test_resolve_accepts_manifest_id_from_containerd_image_store(
        self,
        run: mock.Mock,
    ) -> None:
        run.return_value = {
            "Id": SELECTED_MANIFEST_DIGEST,
            "RepoDigests": [NAMED_DIGEST],
            "Os": "linux",
            "Architecture": "arm64",
            "Config": {"Volumes": None},
        }
        resolved = resolve_submitted_image(
            ("docker",),
            NAMED_DIGEST,
            _size_evidence(),
        )
        self.assertEqual(
            SELECTED_MANIFEST_DIGEST,
            resolved.local_image_id,
        )
        self.assertEqual(NAMED_DIGEST, resolved.execution_image_reference)

    @mock.patch("ossp_router.orchestrator.inspect_image_runtime_metadata")
    def test_resolve_submitted_image_rejects_digest_mismatch(
        self,
        run: mock.Mock,
    ) -> None:
        run.return_value = {
            "Id": LOCAL_IMAGE_ID,
            "RepoDigests": [
                "registry.example/other@sha256:" + "d" * 64
            ],
            "Os": "linux",
            "Architecture": "arm64",
            "Config": {"Volumes": None},
        }
        with self.assertRaises(InfrastructureUnavailable):
            resolve_submitted_image(
                ("docker",),
                NAMED_DIGEST,
                _size_evidence(),
            )

    @mock.patch("ossp_router.orchestrator.inspect_image_runtime_metadata")
    def test_resolve_submitted_image_rejects_declared_volume(
        self,
        run: mock.Mock,
    ) -> None:
        run.return_value = {
            "Id": LOCAL_IMAGE_ID,
            "RepoDigests": [NAMED_DIGEST],
            "Os": "linux",
            "Architecture": "arm64",
            "Config": {
                "Volumes": {"/challenge/output/hidden": {}},
            },
        }
        with self.assertRaisesRegex(ValueError, "VOLUME") as raised:
            resolve_submitted_image(
                ("docker",),
                NAMED_DIGEST,
                _size_evidence(),
            )
        self.assertNotIn(
            "/challenge/output/hidden",
            str(raised.exception),
        )

    @mock.patch("ossp_router.orchestrator.inspect_image_runtime_metadata")
    def test_resolve_submitted_image_fails_closed_without_size_evidence(
        self,
        run: mock.Mock,
    ) -> None:
        with self.assertRaisesRegex(
            InfrastructureUnavailable,
            "크기 증거",
        ):
            resolve_submitted_image(("docker",), NAMED_DIGEST)
        run.assert_not_called()

    @mock.patch("ossp_router.orchestrator.inspect_image_runtime_metadata")
    def test_resolve_submitted_image_rejects_oversized_compressed_layers(
        self,
        run: mock.Mock,
    ) -> None:
        run.return_value = {
            "Id": LOCAL_IMAGE_ID,
            "RepoDigests": [NAMED_DIGEST],
            "Os": "linux",
            "Architecture": "arm64",
            "Config": {"Volumes": None},
        }
        evidence = _size_evidence(
            compressed=(
                PHASE_C_CANDIDATE_LIMITS.oci_compressed_image_bytes + 1
            )
        )
        with self.assertRaisesRegex(ValueError, "압축 계층 합계"):
            resolve_submitted_image(("docker",), NAMED_DIGEST, evidence)

    @mock.patch("ossp_router.orchestrator.inspect_image_runtime_metadata")
    def test_resolve_submitted_image_rejects_oversized_rootfs(
        self,
        run: mock.Mock,
    ) -> None:
        run.return_value = {
            "Id": LOCAL_IMAGE_ID,
            "RepoDigests": [NAMED_DIGEST],
            "Os": "linux",
            "Architecture": "arm64",
            "Config": {"Volumes": None},
        }
        evidence = _size_evidence(
            rootfs=PHASE_C_CANDIDATE_LIMITS.rootfs_apparent_bytes + 1
        )
        with self.assertRaisesRegex(ValueError, "rootfs"):
            resolve_submitted_image(("docker",), NAMED_DIGEST, evidence)

    @mock.patch("ossp_router.orchestrator.inspect_image_runtime_metadata")
    def test_resolve_submitted_image_rejects_evidence_for_other_local_image(
        self,
        run: mock.Mock,
    ) -> None:
        run.return_value = {
            "Id": "sha256:" + "e" * 64,
            "RepoDigests": [NAMED_DIGEST],
            "Os": "linux",
            "Architecture": "arm64",
            "Config": {"Volumes": None},
        }
        with self.assertRaisesRegex(
            InfrastructureUnavailable,
            "대응",
        ):
            resolve_submitted_image(
                ("docker",),
                NAMED_DIGEST,
                _size_evidence(),
            )

    def test_official_image_size_rejection_has_distinct_record_status(
        self,
    ) -> None:
        inputs = _inputs()
        policy = load_bundled_policy()

        def resolver(_runtime, submitted, _evidence):
            return ResolvedImage(
                submitted,
                LOCAL_IMAGE_ID,
                "linux",
                "arm64",
                SELECTED_MANIFEST_DIGEST,
                PHASE_C_CANDIDATE_LIMITS.oci_compressed_image_bytes + 1,
                2000,
                "f" * 64,
            )

        with tempfile.TemporaryDirectory() as temporary:
            target = pathlib.Path(temporary)
            input_path = target / "inputs.json"
            write_json(
                input_path,
                {
                    "schema_version": 1,
                    "challenge_id": inputs.challenge_id,
                    "split": inputs.split,
                    "episodes": [
                        {
                            "episode_id": episode.episode_id,
                            "prompt": episode.prompt,
                        }
                        for episode in inputs.episodes
                    ],
                },
            )
            record_path = target / "run" / "execution-record.json"
            with self.assertRaises(ImageLimitExceeded):
                run_official_tier(
                    runtime_command=("docker",),
                    submitted_image_digest=NAMED_DIGEST,
                    commit_sha=COMMIT_SHA,
                    inputs=load_input(input_path),
                    policy=policy,
                    input_path=input_path,
                    output_directory=target / "run",
                    private_work_directory=_private_work_directory(target),
                    tier="fast",
                    image_resolver=resolver,
                    record_path=record_path,
                )
            record = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual(
                "submission_preflight_rejected",
                record["status"],
            )
            self.assertEqual(
                "image_size_limit_exceeded",
                record["reason_code"],
            )

    def test_declared_volume_is_submission_preflight_rejection(self) -> None:
        inputs = _inputs()
        policy = load_bundled_policy()

        def resolver(_runtime, _submitted, _evidence):
            raise DeclaredVolumeNotAllowed("VOLUME")

        with tempfile.TemporaryDirectory() as temporary:
            target = pathlib.Path(temporary)
            input_path = target / "inputs.json"
            write_json(
                input_path,
                {
                    "schema_version": 1,
                    "challenge_id": inputs.challenge_id,
                    "split": inputs.split,
                    "episodes": [
                        {
                            "episode_id": episode.episode_id,
                            "prompt": episode.prompt,
                        }
                        for episode in inputs.episodes
                    ],
                },
            )
            record_path = target / "run" / "execution-record.json"
            with self.assertRaises(DeclaredVolumeNotAllowed):
                run_official_tier(
                    runtime_command=("docker",),
                    submitted_image_digest=NAMED_DIGEST,
                    commit_sha=COMMIT_SHA,
                    inputs=load_input(input_path),
                    policy=policy,
                    input_path=input_path,
                    output_directory=target / "run",
                    private_work_directory=_private_work_directory(target),
                    tier="fast",
                    image_resolver=resolver,
                    record_path=record_path,
                )
            record = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual(
                "submission_preflight_rejected",
                record["status"],
            )
            self.assertEqual(
                "declared_volume_not_allowed",
                record["reason_code"],
            )

    def test_audit_variant_deranges_same_id_set_and_preserves_occurrences(
        self,
    ) -> None:
        inputs = _inputs()
        variant = make_id_order_audit_variant(inputs, secret=b"x" * 32)
        original_ids = [episode.episode_id for episode in inputs.episodes]
        audit_ids = [episode.episode_id for episode in variant.inputs.episodes]
        self.assertEqual(set(original_ids), set(audit_ids))
        self.assertTrue(all(
            original_id != audit_id
            for original_id, audit_id in variant.original_to_audit_id
        ))
        self.assertEqual(
            sorted(episode.prompt for episode in inputs.episodes),
            sorted(episode.prompt for episode in variant.inputs.episodes),
        )
        self.assertEqual(3, len(variant.original_to_audit_id))

    def test_single_episode_audit_keeps_original_id_without_marker(self) -> None:
        inputs = InputBatch(
            schema_version=1,
            challenge_id="single-audit",
            split="synthetic",
            episodes=(Episode("original-shape-001", prompt="hello"),),
        )
        variant = make_id_order_audit_variant(inputs, secret=b"x" * 32)
        self.assertEqual("original-shape-001", variant.inputs.episodes[0].episode_id)
        self.assertEqual(
            (("original-shape-001", "original-shape-001"),),
            variant.original_to_audit_id,
        )

    def test_duplicate_prompt_occurrence_mismatch_is_not_hidden(self) -> None:
        inputs = _inputs()
        policy = load_bundled_policy()

        def attempt(variant_inputs: InputBatch, _number: int) -> AttemptResult:
            decisions = [
                Decision(episode.episode_id, "ax31-light")
                for episode in variant_inputs.episodes
            ]
            decisions[0] = Decision(decisions[0].episode_id, "ax31")
            submission = Submission(
                1,
                inputs.challenge_id,
                policy.policy_id,
                inputs.split,
                "fast",
                tuple(decisions),
            )
            return AttemptResult(
                AttemptKind.VALID,
                "audit",
                submission=submission,
            )

        outcome = run_id_order_audit(
            original_inputs=inputs,
            official_submission=_submission(inputs),
            policy=policy,
            tier="fast",
            attempt=attempt,
            secret=b"y" * 32,
        )
        self.assertEqual(AuditStatus.REVIEW_REQUIRED, outcome.status)
        self.assertEqual(1, len(outcome.mismatches))

    def test_audit_infrastructure_failures_are_inconclusive(self) -> None:
        calls = []

        def attempt(_inputs: InputBatch, number: int) -> AttemptResult:
            calls.append(number)
            return AttemptResult(
                AttemptKind.INFRASTRUCTURE_FAILURE,
                "daemon unavailable",
            )

        outcome = run_id_order_audit(
            original_inputs=_inputs(),
            official_submission=_submission(_inputs()),
            policy=load_bundled_policy(),
            tier="fast",
            attempt=attempt,
            secret=b"z" * 32,
            max_infrastructure_failures=3,
        )
        self.assertEqual([1, 2, 3], calls)
        self.assertEqual(
            AuditStatus.INCONCLUSIVE_INFRASTRUCTURE,
            outcome.status,
        )

    def test_audit_cleanup_pending_stops_immediately(self) -> None:
        calls = []

        def attempt(_inputs: InputBatch, number: int) -> AttemptResult:
            calls.append(number)
            return AttemptResult(
                AttemptKind.INFRASTRUCTURE_FAILURE,
                "cleanup pending",
                cleanup_pending=True,
            )

        outcome = run_id_order_audit(
            original_inputs=_inputs(),
            official_submission=_submission(_inputs()),
            policy=load_bundled_policy(),
            tier="fast",
            attempt=attempt,
            secret=b"z" * 32,
        )
        self.assertEqual([1], calls)
        self.assertEqual(
            AuditStatus.INCONCLUSIVE_INFRASTRUCTURE,
            outcome.status,
        )
        self.assertEqual(1, outcome.infrastructure_failures)
        record = _attempt_to_dict(outcome.history[0], sequence=1)
        self.assertTrue(record["cleanup_pending"])
        self.assertEqual(
            "operator_cleanup_pending",
            record["reason_code"],
        )

    def test_official_retry_and_audit_use_one_resolved_image(self) -> None:
        inputs = _inputs()
        policy = load_bundled_policy()
        calls = []
        mounted_inputs = []

        def resolver(_runtime, submitted, _evidence):
            return _resolved_image(
                submitted,
                execution_image_reference=submitted,
            )

        def runner(_runtime, **kwargs):
            mounted_path = kwargs["input_path"]
            calls.append(
                (
                    kwargs["image"],
                    kwargs["platform"],
                    kwargs["inputs"],
                    mounted_path,
                )
            )
            mounted_inputs.append(
                {
                    "path": mounted_path,
                    "payload": mounted_path.read_bytes(),
                    "file_mode": mounted_path.stat().st_mode & 0o777,
                    "directory_mode": mounted_path.parent.stat().st_mode & 0o777,
                }
            )
            if len(calls) == 1:
                return AttemptResult(
                    AttemptKind.INFRASTRUCTURE_FAILURE,
                    "temporary daemon failure",
                )
            return AttemptResult(
                AttemptKind.VALID,
                "valid",
                submission=_submission(kwargs["inputs"], kwargs["tier"]),
                returncode=0,
                output_bytes=123,
                output_sha256="e" * 64,
            )

        with tempfile.TemporaryDirectory() as temporary:
            target = pathlib.Path(temporary)
            input_path = target / "inputs.json"
            write_json(
                input_path,
                {
                    "schema_version": 1,
                    "challenge_id": inputs.challenge_id,
                    "split": inputs.split,
                    "episodes": [
                        {
                            "episode_id": episode.episode_id,
                            "prompt": episode.prompt,
                        }
                        for episode in inputs.episodes
                    ],
                },
            )
            loaded = load_input(input_path)
            original_payload = input_path.read_bytes()
            outcome = run_official_tier(
                runtime_command=("docker",),
                submitted_image_digest=NAMED_DIGEST,
                commit_sha=COMMIT_SHA,
                inputs=loaded,
                policy=policy,
                input_path=input_path,
                output_directory=target / "run",
                private_work_directory=_private_work_directory(target),
                tier="fast",
                audit=True,
                audit_secret=b"audit-secret-value",
                image_resolver=resolver,
                container_runner=runner,
            )
            audit_file_sha256 = hashlib.sha256(
                mounted_inputs[-1]["payload"]
            ).hexdigest()
            self.assertFalse((target / "run" / "audit" / "inputs.json").exists())

        self.assertEqual(3, len(calls))
        self.assertEqual({NAMED_DIGEST}, {image for image, _, _, _ in calls})
        self.assertEqual(
            {"linux/arm64"},
            {platform for _, platform, _, _ in calls},
        )
        self.assertEqual(calls[0][3], calls[1][3])
        self.assertNotEqual(calls[1][3], calls[2][3])
        self.assertEqual(
            [original_payload, original_payload],
            [item["payload"] for item in mounted_inputs[:2]],
        )
        self.assertEqual([0o444] * 3, [
            item["file_mode"] for item in mounted_inputs
        ])
        self.assertEqual([0o700] * 3, [
            item["directory_mode"] for item in mounted_inputs
        ])
        self.assertTrue(all(
            not item["path"].exists() for item in mounted_inputs
        ))
        self.assertEqual(1, outcome.retry.official_attempts)
        self.assertEqual(1, outcome.retry.infrastructure_failures)
        self.assertEqual(
            hashlib.sha256(original_payload).hexdigest(),
            outcome.input_sha256,
        )
        self.assertEqual(AuditStatus.PASSED, outcome.audit.status)
        record = official_tier_outcome_to_dict(outcome)
        self.assertEqual(
            SELECTED_MANIFEST_DIGEST,
            record["image"]["selected_manifest_digest"],
        )
        self.assertEqual(1000, record["image"]["oci_compressed_layer_bytes"])
        self.assertEqual(2000, record["image"]["rootfs_apparent_bytes"])
        self.assertEqual("f" * 64, record["image"]["size_evidence_sha256"])
        self.assertEqual([1, 1], [
            item["official_attempt"] for item in record["history"]
        ])
        self.assertNotIn("stdout", record["history"][0])
        self.assertNotIn("stderr", record["history"][0])
        self.assertFalse(record["history"][0]["cleanup_pending"])
        self.assertEqual(
            hashlib.sha256(b"").hexdigest(),
            record["history"][0]["stdout_normalized_utf8_sha256"],
        )
        self.assertEqual("passed", record["audit"]["status"])
        self.assertEqual(
            audit_file_sha256,
            record["audit"]["input_sha256"],
        )
        self.assertEqual(
            len(inputs.episodes),
            len(record["audit"]["episode_id_mapping"]),
        )

    def test_official_and_audit_share_one_output_root_lock(self) -> None:
        inputs = _inputs()
        policy = load_bundled_policy()
        with tempfile.TemporaryDirectory() as temporary:
            target = pathlib.Path(temporary)
            input_path = target / "inputs.json"
            write_json(
                input_path,
                {
                    "schema_version": 1,
                    "challenge_id": inputs.challenge_id,
                    "split": inputs.split,
                    "episodes": [
                        {
                            "episode_id": episode.episode_id,
                            "prompt": episode.prompt,
                        }
                        for episode in inputs.episodes
                    ],
                },
            )
            loaded = load_input(input_path)
            output_directory = target / "run"
            output_directory.mkdir(mode=0o700)
            resolver = mock.Mock()
            with exclusive_evaluation_lock(output_directory):
                with self.assertRaises(ResourceLockUnavailable):
                    run_official_tier(
                        runtime_command=("docker",),
                        submitted_image_digest=NAMED_DIGEST,
                        commit_sha=COMMIT_SHA,
                        inputs=loaded,
                        policy=policy,
                        input_path=input_path,
                        output_directory=output_directory,
                        private_work_directory=_private_work_directory(target),
                        tier="fast",
                        image_resolver=resolver,
                    )
        resolver.assert_not_called()

    def test_official_paths_are_pinned_to_canonical_alias_targets(self) -> None:
        inputs = _inputs()
        policy = load_bundled_policy()
        observed = {}

        def resolver(_runtime, submitted, _evidence):
            return _resolved_image(
                submitted,
                execution_image_reference=submitted,
            )

        def runner(_runtime, **kwargs):
            observed["output_directory"] = kwargs["output_directory"]
            observed["input_path"] = kwargs["input_path"]
            return AttemptResult(
                AttemptKind.VALID,
                "valid",
                submission=_submission(kwargs["inputs"], kwargs["tier"]),
                returncode=0,
            )

        with tempfile.TemporaryDirectory() as temporary:
            target = pathlib.Path(temporary)
            input_path = target / "inputs.json"
            write_json(
                input_path,
                {
                    "schema_version": 1,
                    "challenge_id": inputs.challenge_id,
                    "split": inputs.split,
                    "episodes": [
                        {
                            "episode_id": episode.episode_id,
                            "prompt": episode.prompt,
                        }
                        for episode in inputs.episodes
                    ],
                },
            )
            canonical_output = target / "canonical-output"
            canonical_output.mkdir(mode=0o700)
            output_alias = target / "output-alias"
            output_alias.symlink_to(
                canonical_output,
                target_is_directory=True,
            )
            canonical_private = target / "canonical-private"
            canonical_private.mkdir(mode=0o700)
            private_alias = target / "private-alias"
            private_alias.symlink_to(
                canonical_private,
                target_is_directory=True,
            )
            record_alias = output_alias / "execution-record.json"

            with mock.patch(
                "ossp_router.orchestrator._write_record_atomic",
                wraps=_write_record_atomic,
            ) as write_record:
                run_official_tier(
                    runtime_command=("docker",),
                    submitted_image_digest=NAMED_DIGEST,
                    commit_sha=COMMIT_SHA,
                    inputs=load_input(input_path),
                    policy=policy,
                    input_path=input_path,
                    output_directory=output_alias,
                    private_work_directory=private_alias,
                    tier="fast",
                    audit=False,
                    image_resolver=resolver,
                    container_runner=runner,
                    record_path=record_alias,
                )

            self.assertEqual(
                (canonical_output / "official").resolve(),
                observed["output_directory"],
            )
            with self.assertRaises(ValueError):
                observed["input_path"].relative_to(private_alias)
            observed["input_path"].relative_to(canonical_private.resolve())
            self.assertEqual(
                canonical_output.resolve() / "execution-record.json",
                write_record.call_args.args[0],
            )
            self.assertTrue(
                (canonical_output / "execution-record.json").is_file()
            )

    def test_official_output_children_cannot_escape_through_symlinks(self) -> None:
        inputs = _inputs()
        policy = load_bundled_policy()

        for child_name in ("official", "audit"):
            with self.subTest(child=child_name):
                resolver = mock.Mock()
                with tempfile.TemporaryDirectory() as temporary:
                    target = pathlib.Path(temporary)
                    input_path = target / "inputs.json"
                    write_json(
                        input_path,
                        {
                            "schema_version": 1,
                            "challenge_id": inputs.challenge_id,
                            "split": inputs.split,
                            "episodes": [
                                {
                                    "episode_id": episode.episode_id,
                                    "prompt": episode.prompt,
                                }
                                for episode in inputs.episodes
                            ],
                        },
                    )
                    output_directory = target / "run"
                    output_directory.mkdir(mode=0o700)
                    private_work = _private_work_directory(target)
                    (output_directory / child_name).symlink_to(
                        private_work,
                        target_is_directory=True,
                    )

                    with self.assertRaisesRegex(
                        InfrastructureUnavailable,
                        "출력 루트 바로 아래의 실제 디렉터리",
                    ):
                        run_official_tier(
                            runtime_command=("docker",),
                            submitted_image_digest=NAMED_DIGEST,
                            commit_sha=COMMIT_SHA,
                            inputs=load_input(input_path),
                            policy=policy,
                            input_path=input_path,
                            output_directory=output_directory,
                            private_work_directory=private_work,
                            tier="fast",
                            image_resolver=resolver,
                        )

                    resolver.assert_not_called()
                    self.assertEqual([], list(private_work.iterdir()))

    def test_official_retries_use_one_private_input_snapshot(self) -> None:
        inputs = _inputs()
        policy = load_bundled_policy()
        mounted_paths = []
        mounted_payloads = []

        def resolver(_runtime, image, _evidence):
            return _resolved_image(image)

        with tempfile.TemporaryDirectory() as temporary:
            target = pathlib.Path(temporary)
            input_path = target / "inputs.json"
            input_value = {
                "schema_version": 1,
                "challenge_id": inputs.challenge_id,
                "split": inputs.split,
                "episodes": [
                    {
                        "episode_id": episode.episode_id,
                        "prompt": episode.prompt,
                    }
                    for episode in inputs.episodes
                ],
            }
            write_json(input_path, input_value)
            original_payload = input_path.read_bytes()
            loaded = load_input(input_path)

            def runner(_runtime, **kwargs):
                mounted_path = kwargs["input_path"]
                mounted_paths.append(mounted_path)
                mounted_payloads.append(mounted_path.read_bytes())
                if len(mounted_paths) == 1:
                    changed = dict(input_value)
                    changed["split"] = "replaced-during-run"
                    write_json(input_path, changed)
                    return AttemptResult(
                        AttemptKind.EXECUTION_FAILURE,
                        "participant retry",
                        returncode=1,
                    )
                return AttemptResult(
                    AttemptKind.VALID,
                    "valid",
                    submission=_submission(kwargs["inputs"], kwargs["tier"]),
                    returncode=0,
                )

            outcome = run_official_tier(
                runtime_command=("docker",),
                submitted_image_digest=NAMED_DIGEST,
                commit_sha=COMMIT_SHA,
                inputs=loaded,
                policy=policy,
                input_path=input_path,
                output_directory=target / "run",
                private_work_directory=_private_work_directory(target),
                tier="fast",
                audit=False,
                image_resolver=resolver,
                container_runner=runner,
            )

        self.assertEqual(2, len(mounted_paths))
        self.assertEqual(mounted_paths[0], mounted_paths[1])
        self.assertEqual(
            [original_payload, original_payload],
            mounted_payloads,
        )
        self.assertTrue(all(not path.exists() for path in mounted_paths))
        self.assertEqual(
            hashlib.sha256(original_payload).hexdigest(),
            outcome.input_sha256,
        )
        self.assertEqual(
            "audit_skipped",
            official_tier_outcome_to_dict(outcome)["status"],
        )

    def test_private_work_residue_blocks_new_evaluation(self) -> None:
        inputs = _inputs()
        policy = load_bundled_policy()
        resolver = mock.Mock()
        with tempfile.TemporaryDirectory() as temporary:
            target = pathlib.Path(temporary)
            input_path = target / "inputs.json"
            write_json(
                input_path,
                {
                    "schema_version": 1,
                    "challenge_id": inputs.challenge_id,
                    "split": inputs.split,
                    "episodes": [
                        {
                            "episode_id": episode.episode_id,
                            "prompt": episode.prompt,
                        }
                        for episode in inputs.episodes
                    ],
                },
            )
            private_work = _private_work_directory(target)
            residue = private_work / ".ossp-router-private-input-stale"
            residue.mkdir(mode=0o700)
            with self.assertRaisesRegex(
                InfrastructureUnavailable,
                "잔여물",
            ):
                run_official_tier(
                    runtime_command=("docker",),
                    submitted_image_digest=NAMED_DIGEST,
                    commit_sha=COMMIT_SHA,
                    inputs=load_input(input_path),
                    policy=policy,
                    input_path=input_path,
                    output_directory=target / "run",
                    private_work_directory=private_work,
                    tier="fast",
                    image_resolver=resolver,
                )
            self.assertTrue(residue.is_dir())
        resolver.assert_not_called()

    def test_private_work_directory_cannot_overlap_results(self) -> None:
        inputs = _inputs()
        policy = load_bundled_policy()
        resolver = mock.Mock()
        with tempfile.TemporaryDirectory() as temporary:
            target = pathlib.Path(temporary)
            input_path = target / "inputs.json"
            write_json(
                input_path,
                {
                    "schema_version": 1,
                    "challenge_id": inputs.challenge_id,
                    "split": inputs.split,
                    "episodes": [
                        {
                            "episode_id": episode.episode_id,
                            "prompt": episode.prompt,
                        }
                        for episode in inputs.episodes
                    ],
                },
            )
            output_directory = target / "run"
            output_directory.mkdir(mode=0o700)
            private_work = output_directory / "private-work"
            private_work.mkdir(mode=0o700)
            with self.assertRaisesRegex(
                InfrastructureUnavailable,
                "서로 분리",
            ):
                run_official_tier(
                    runtime_command=("docker",),
                    submitted_image_digest=NAMED_DIGEST,
                    commit_sha=COMMIT_SHA,
                    inputs=load_input(input_path),
                    policy=policy,
                    input_path=input_path,
                    output_directory=output_directory,
                    private_work_directory=private_work,
                    tier="fast",
                    image_resolver=resolver,
                )
        resolver.assert_not_called()

    def test_sibling_journals_are_checked_before_image_resolution(self) -> None:
        inputs = _inputs()
        policy = load_bundled_policy()
        resolver = mock.Mock()
        with tempfile.TemporaryDirectory() as temporary:
            target = pathlib.Path(temporary)
            input_path = target / "inputs.json"
            write_json(
                input_path,
                {
                    "schema_version": 1,
                    "challenge_id": inputs.challenge_id,
                    "split": inputs.split,
                    "episodes": [
                        {
                            "episode_id": episode.episode_id,
                            "prompt": episode.prompt,
                        }
                        for episode in inputs.episodes
                    ],
                },
            )
            loaded = load_input(input_path)
            output_directory = target / "run"
            with mock.patch(
                "ossp_router.orchestrator.require_no_pending_runtime_resources",
                side_effect=PendingRuntimeResources("audit cleanup pending"),
            ) as preflight:
                with self.assertRaises(InfrastructureUnavailable):
                    run_official_tier(
                        runtime_command=("docker",),
                        submitted_image_digest=NAMED_DIGEST,
                        commit_sha=COMMIT_SHA,
                        inputs=loaded,
                        policy=policy,
                        input_path=input_path,
                        output_directory=output_directory,
                        private_work_directory=_private_work_directory(target),
                        tier="fast",
                        image_resolver=resolver,
                    )
            checked = preflight.call_args.args[1]
            self.assertEqual(
                ["official", "audit"],
                [path.name for path in checked],
            )
        resolver.assert_not_called()

    def test_sibling_cleanup_journal_preserves_detailed_record(self) -> None:
        inputs = _inputs()
        policy = load_bundled_policy()
        resolver = mock.Mock()
        with tempfile.TemporaryDirectory() as temporary:
            target = pathlib.Path(temporary)
            input_path = target / "inputs.json"
            write_json(
                input_path,
                {
                    "schema_version": 1,
                    "challenge_id": inputs.challenge_id,
                    "split": inputs.split,
                    "episodes": [
                        {
                            "episode_id": episode.episode_id,
                            "prompt": episode.prompt,
                        }
                        for episode in inputs.episodes
                    ],
                },
            )
            loaded = load_input(input_path)
            output_directory = target / "run"
            audit_directory = output_directory / "audit"
            audit_directory.mkdir(mode=0o700, parents=True)
            output_directory.chmod(0o700)
            token = "d" * 32
            _write_resource_journal(
                audit_directory,
                _ResourceJournal(
                    attempt_token=token,
                    participant_name=f"ossp-router-participant-{token}",
                    helper_name="ossp-router-helper-audit-record",
                    volume_name="ossp-router-output-audit-record",
                    runtime_command=("docker",),
                    daemon_id="phase-c-test-daemon",
                    observed_roles=("participant", "helper", "volume"),
                ),
            )
            record_path = output_directory / "execution-record.json"
            detailed_record = {
                "schema_version": 1,
                "report_type": "official-tier-execution-record",
                "status": "infrastructure_unavailable",
                "cleanup_pending": True,
                "history": [
                    {
                        "reason_code": "operator_cleanup_pending",
                        "cleanup_pending": True,
                        "cleanup_attempt_id": token,
                    }
                ],
            }
            write_json(record_path, detailed_record)
            with self.assertRaises(PendingRuntimeResources):
                run_official_tier(
                    runtime_command=("docker",),
                    submitted_image_digest=NAMED_DIGEST,
                    commit_sha=COMMIT_SHA,
                    inputs=loaded,
                    policy=policy,
                    input_path=input_path,
                    output_directory=output_directory,
                    private_work_directory=_private_work_directory(target),
                    tier="fast",
                    image_resolver=resolver,
                    record_path=record_path,
                )
            self.assertEqual(
                detailed_record,
                json.loads(record_path.read_text(encoding="utf-8")),
            )
        resolver.assert_not_called()

    def test_sibling_cleanup_replaces_stale_valid_record(self) -> None:
        inputs = _inputs()
        policy = load_bundled_policy()
        resolver = mock.Mock()
        with tempfile.TemporaryDirectory() as temporary:
            target = pathlib.Path(temporary)
            input_path = target / "inputs.json"
            write_json(
                input_path,
                {
                    "schema_version": 1,
                    "challenge_id": inputs.challenge_id,
                    "split": inputs.split,
                    "episodes": [
                        {
                            "episode_id": episode.episode_id,
                            "prompt": episode.prompt,
                        }
                        for episode in inputs.episodes
                    ],
                },
            )
            loaded = load_input(input_path)
            output_directory = target / "run"
            official_directory = output_directory / "official"
            official_directory.mkdir(mode=0o700, parents=True)
            output_directory.chmod(0o700)
            token = "e" * 32
            _write_resource_journal(
                official_directory,
                _ResourceJournal(
                    attempt_token=token,
                    participant_name=f"ossp-router-participant-{token}",
                    helper_name="ossp-router-helper-stale-record",
                    volume_name="ossp-router-output-stale-record",
                    runtime_command=("docker",),
                    daemon_id="phase-c-test-daemon",
                    observed_roles=("participant",),
                ),
            )
            record_path = output_directory / "execution-record.json"
            write_json(
                record_path,
                {
                    "schema_version": 1,
                    "report_type": "official-tier-execution-record",
                    "status": "valid",
                    "stale_marker": True,
                },
            )
            with self.assertRaises(PendingRuntimeResources):
                run_official_tier(
                    runtime_command=("docker",),
                    submitted_image_digest=NAMED_DIGEST,
                    commit_sha=COMMIT_SHA,
                    inputs=loaded,
                    policy=policy,
                    input_path=input_path,
                    output_directory=output_directory,
                    private_work_directory=_private_work_directory(target),
                    tier="fast",
                    image_resolver=resolver,
                    record_path=record_path,
                )
            record = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual("infrastructure_unavailable", record["status"])
            self.assertEqual("operator_cleanup_pending", record["reason_code"])
            self.assertEqual(token, record["cleanup_attempt_id"])
            self.assertTrue(record["cleanup_pending"])
            self.assertNotIn("stale_marker", record)
        resolver.assert_not_called()

    def test_sibling_cleanup_without_record_creates_pending_record(self) -> None:
        inputs = _inputs()
        policy = load_bundled_policy()
        with tempfile.TemporaryDirectory() as temporary:
            target = pathlib.Path(temporary)
            input_path = target / "inputs.json"
            write_json(
                input_path,
                {
                    "schema_version": 1,
                    "challenge_id": inputs.challenge_id,
                    "split": inputs.split,
                    "episodes": [
                        {
                            "episode_id": episode.episode_id,
                            "prompt": episode.prompt,
                        }
                        for episode in inputs.episodes
                    ],
                },
            )
            loaded = load_input(input_path)
            output_directory = target / "run"
            record_path = output_directory / "execution-record.json"
            with mock.patch(
                "ossp_router.orchestrator.require_no_pending_runtime_resources",
                side_effect=PendingRuntimeResources("cleanup pending"),
            ):
                with self.assertRaises(PendingRuntimeResources):
                    run_official_tier(
                        runtime_command=("docker",),
                        submitted_image_digest=NAMED_DIGEST,
                        commit_sha=COMMIT_SHA,
                        inputs=loaded,
                        policy=policy,
                        input_path=input_path,
                        output_directory=output_directory,
                        private_work_directory=_private_work_directory(target),
                        tier="fast",
                        record_path=record_path,
                    )
            record = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual("operator_cleanup_pending", record["reason_code"])
            self.assertTrue(record["cleanup_pending"])

    def test_record_is_published_atomically_while_root_lock_is_held(self) -> None:
        inputs = _inputs()
        policy = load_bundled_policy()

        def resolver(_runtime, image, _evidence):
            return _resolved_image(image)

        def runner(_runtime, **kwargs):
            return AttemptResult(
                AttemptKind.VALID,
                "valid",
                submission=_submission(kwargs["inputs"], kwargs["tier"]),
                returncode=0,
            )

        with tempfile.TemporaryDirectory() as temporary:
            target = pathlib.Path(temporary)
            input_path = target / "inputs.json"
            write_json(
                input_path,
                {
                    "schema_version": 1,
                    "challenge_id": inputs.challenge_id,
                    "split": inputs.split,
                    "episodes": [
                        {
                            "episode_id": episode.episode_id,
                            "prompt": episode.prompt,
                        }
                        for episode in inputs.episodes
                    ],
                },
            )
            loaded = load_input(input_path)
            output_directory = target / "run"
            record_path = output_directory / "execution-record.json"

            def checked_write(path, value):
                with self.assertRaises(ResourceLockUnavailable):
                    with exclusive_evaluation_lock(output_directory):
                        pass
                _write_record_atomic(path, value)

            with mock.patch(
                "ossp_router.orchestrator._write_record_atomic",
                side_effect=checked_write,
            ):
                run_official_tier(
                    runtime_command=("docker",),
                    submitted_image_digest=NAMED_DIGEST,
                    commit_sha=COMMIT_SHA,
                    inputs=loaded,
                    policy=policy,
                    input_path=input_path,
                    output_directory=output_directory,
                    private_work_directory=_private_work_directory(target),
                    tier="fast",
                    audit=False,
                    image_resolver=resolver,
                    container_runner=runner,
                    record_path=record_path,
                )
            record = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual("official-tier-execution-record", record["report_type"])
            self.assertFalse(
                list(output_directory.glob(".execution-record.json.operator-*"))
            )

    def test_lock_contender_does_not_overwrite_existing_record(self) -> None:
        inputs = _inputs()
        with tempfile.TemporaryDirectory() as temporary:
            target = pathlib.Path(temporary)
            input_path = target / "inputs.json"
            write_json(
                input_path,
                {
                    "schema_version": 1,
                    "challenge_id": inputs.challenge_id,
                    "split": inputs.split,
                    "episodes": [
                        {
                            "episode_id": episode.episode_id,
                            "prompt": episode.prompt,
                        }
                        for episode in inputs.episodes
                    ],
                },
            )
            output_directory = target / "run"
            output_directory.mkdir(mode=0o700)
            record_path = output_directory / "execution-record.json"
            record_path.write_text('{"sentinel":true}\n', encoding="utf-8")
            with exclusive_evaluation_lock(output_directory):
                returncode = main(
                    [
                        "--image",
                        NAMED_DIGEST,
                        "--commit-sha",
                        COMMIT_SHA,
                        "--input",
                        str(input_path),
                        "--tier",
                        "fast",
                        "--output-directory",
                        str(output_directory),
                        "--private-work-directory",
                        str(target / "private-work"),
                        "--image-size-evidence",
                        str(_image_size_evidence_path(target)),
                        "--record",
                        str(record_path),
                    ]
                )
            self.assertEqual(4, returncode)
            self.assertEqual(
                {"sentinel": True},
                json.loads(record_path.read_text(encoding="utf-8")),
            )

    def test_repeated_official_infrastructure_failure_keeps_record(self) -> None:
        inputs = _inputs()
        policy = load_bundled_policy()

        def resolver(_runtime, image, _evidence):
            return _resolved_image(image)

        def runner(*_args, **_kwargs):
            return AttemptResult(
                AttemptKind.INFRASTRUCTURE_FAILURE,
                "operator unavailable SECRET-PROMPT-MARKER",
                returncode=125,
                stdout="SECRET-PROMPT-MARKER",
                stderr="SECRET-PROMPT-MARKER",
            )

        with tempfile.TemporaryDirectory() as temporary:
            target = pathlib.Path(temporary)
            input_path = target / "inputs.json"
            write_json(
                input_path,
                {
                    "schema_version": 1,
                    "challenge_id": inputs.challenge_id,
                    "split": inputs.split,
                    "episodes": [
                        {
                            "episode_id": episode.episode_id,
                            "prompt": episode.prompt,
                        }
                        for episode in inputs.episodes
                    ],
                },
            )
            loaded = load_input(input_path)
            outcome = run_official_tier(
                runtime_command=("docker",),
                submitted_image_digest=NAMED_DIGEST,
                commit_sha=COMMIT_SHA,
                inputs=loaded,
                policy=policy,
                input_path=input_path,
                output_directory=target / "run",
                private_work_directory=_private_work_directory(target),
                tier="fast",
                image_resolver=resolver,
                container_runner=runner,
            )

        record = official_tier_outcome_to_dict(outcome)
        self.assertEqual("infrastructure_unavailable", record["status"])
        self.assertTrue(record["infrastructure_unavailable"])
        self.assertEqual(3, record["infrastructure_failures"])
        self.assertEqual(3, len(record["history"]))
        self.assertEqual(0, record["official_attempts"])
        self.assertFalse(record["tier_score_zero"])
        self.assertNotIn("SECRET-PROMPT-MARKER", json.dumps(record))

    def test_cli_returns_infrastructure_code_when_audit_is_inconclusive(self) -> None:
        outcome = _cli_outcome(
            audit_status=AuditStatus.INCONCLUSIVE_INFRASTRUCTURE
        )
        with tempfile.TemporaryDirectory() as temporary:
            target = pathlib.Path(temporary)
            record_path = target / "run" / "execution-record.json"
            with mock.patch(
                "ossp_router.orchestrator.run_official_tier",
                return_value=outcome,
            ), mock.patch(
                "ossp_router.orchestrator.official_tier_outcome_to_dict",
                return_value={"status": "audit_inconclusive_infrastructure"},
            ):
                returncode = main(
                    [
                        "--image",
                        NAMED_DIGEST,
                        "--commit-sha",
                        COMMIT_SHA,
                        "--input",
                        str(
                            pathlib.Path(__file__).resolve().parents[1]
                            / "data/toy/inputs.json"
                        ),
                        "--tier",
                        "fast",
                        "--output-directory",
                        str(target / "run"),
                        "--private-work-directory",
                        str(target / "private-work"),
                        "--image-size-evidence",
                        str(_image_size_evidence_path(target)),
                        "--record",
                        str(record_path),
                    ]
                )
        self.assertEqual(4, returncode)

    def test_cli_returns_review_code_when_audit_requires_review(self) -> None:
        outcome = _cli_outcome(audit_status=AuditStatus.REVIEW_REQUIRED)
        with tempfile.TemporaryDirectory() as temporary:
            target = pathlib.Path(temporary)
            with mock.patch(
                "ossp_router.orchestrator.run_official_tier",
                return_value=outcome,
            ):
                returncode = main(
                    [
                        "--image",
                        NAMED_DIGEST,
                        "--commit-sha",
                        COMMIT_SHA,
                        "--input",
                        str(
                            pathlib.Path(__file__).resolve().parents[1]
                            / "data/toy/inputs.json"
                        ),
                        "--tier",
                        "fast",
                        "--output-directory",
                        str(target / "run"),
                        "--private-work-directory",
                        str(target / "private-work"),
                        "--image-size-evidence",
                        str(_image_size_evidence_path(target)),
                        "--record",
                        str(target / "run" / "execution-record.json"),
                    ]
                )
        self.assertEqual(5, returncode)

    def test_cli_returns_success_code_when_audit_passes(self) -> None:
        outcome = _cli_outcome(audit_status=AuditStatus.PASSED)
        with tempfile.TemporaryDirectory() as temporary:
            target = pathlib.Path(temporary)
            with mock.patch(
                "ossp_router.orchestrator.run_official_tier",
                return_value=outcome,
            ):
                returncode = main(
                    [
                        "--image",
                        NAMED_DIGEST,
                        "--commit-sha",
                        COMMIT_SHA,
                        "--input",
                        str(
                            pathlib.Path(__file__).resolve().parents[1]
                            / "data/toy/inputs.json"
                        ),
                        "--tier",
                        "fast",
                        "--output-directory",
                        str(target / "run"),
                        "--private-work-directory",
                        str(target / "private-work"),
                        "--image-size-evidence",
                        str(_image_size_evidence_path(target)),
                        "--record",
                        str(target / "run" / "execution-record.json"),
                    ]
                )
        self.assertEqual(0, returncode)

    def test_cli_fails_closed_when_audit_result_is_missing(self) -> None:
        outcome = _cli_outcome(audit_status=None)
        with tempfile.TemporaryDirectory() as temporary:
            target = pathlib.Path(temporary)
            with mock.patch(
                "ossp_router.orchestrator.run_official_tier",
                return_value=outcome,
            ):
                returncode = main(
                    [
                        "--image",
                        NAMED_DIGEST,
                        "--commit-sha",
                        COMMIT_SHA,
                        "--input",
                        str(
                            pathlib.Path(__file__).resolve().parents[1]
                            / "data/toy/inputs.json"
                        ),
                        "--tier",
                        "fast",
                        "--output-directory",
                        str(target / "run"),
                        "--private-work-directory",
                        str(target / "private-work"),
                        "--image-size-evidence",
                        str(_image_size_evidence_path(target)),
                        "--record",
                        str(target / "run" / "execution-record.json"),
                    ]
                )
        self.assertEqual(5, returncode)

    def test_cli_terminal_results_return_success_code(self) -> None:
        for label, outcome in (
            (
                "tier_score_zero",
                _cli_outcome(
                    audit_status=None,
                    tier_score_zero=True,
                ),
            ),
            (
                "disqualified",
                _cli_outcome(
                    audit_status=None,
                    disqualified=True,
                ),
            ),
        ):
            with self.subTest(status=label):
                with tempfile.TemporaryDirectory() as temporary:
                    target = pathlib.Path(temporary)
                    with mock.patch(
                        "ossp_router.orchestrator.run_official_tier",
                        return_value=outcome,
                    ):
                        returncode = main(
                            [
                                "--image",
                                NAMED_DIGEST,
                                "--commit-sha",
                                COMMIT_SHA,
                                "--input",
                                str(
                                    pathlib.Path(__file__).resolve().parents[1]
                                    / "data/toy/inputs.json"
                                ),
                                "--tier",
                                "fast",
                                "--output-directory",
                                str(target / "run"),
                                "--private-work-directory",
                                str(target / "private-work"),
                                "--image-size-evidence",
                                str(_image_size_evidence_path(target)),
                                "--record",
                                str(
                                    target / "run" / "execution-record.json"
                                ),
                            ]
                        )
                self.assertEqual(0, returncode)

    def test_cli_configuration_error_returns_code_two(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = pathlib.Path(temporary)
            returncode = main(
                [
                    "--image",
                    NAMED_DIGEST,
                    "--commit-sha",
                    COMMIT_SHA,
                    "--input",
                    str(target / "missing-input.json"),
                    "--tier",
                    "fast",
                    "--output-directory",
                    str(target / "run"),
                    "--private-work-directory",
                    str(target / "private-work"),
                    "--image-size-evidence",
                    str(target / "missing-evidence.json"),
                    "--record",
                    str(target / "run" / "execution-record.json"),
                ]
            )
        self.assertEqual(2, returncode)

    def test_cli_image_size_preflight_rejection_returns_code_six(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = pathlib.Path(temporary)
            with mock.patch(
                "ossp_router.orchestrator.run_official_tier",
                side_effect=ImageLimitExceeded("oversized"),
            ):
                returncode = main(
                    [
                        "--image",
                        NAMED_DIGEST,
                        "--commit-sha",
                        COMMIT_SHA,
                        "--input",
                        str(
                            pathlib.Path(__file__).resolve().parents[1]
                            / "data/toy/inputs.json"
                        ),
                        "--tier",
                        "fast",
                        "--output-directory",
                        str(target / "run"),
                        "--private-work-directory",
                        str(target / "private-work"),
                        "--image-size-evidence",
                        str(_image_size_evidence_path(target)),
                        "--record",
                        str(target / "run" / "execution-record.json"),
                    ]
                )
        self.assertEqual(6, returncode)

    def test_official_cli_rejects_skip_audit_option(self) -> None:
        with mock.patch("sys.stderr"):
            with self.assertRaises(SystemExit) as raised:
                main(
                    [
                        "--image",
                        NAMED_DIGEST,
                        "--commit-sha",
                        COMMIT_SHA,
                        "--input",
                        str(
                            pathlib.Path(__file__).resolve().parents[1]
                            / "data/toy/inputs.json"
                        ),
                        "--tier",
                        "fast",
                        "--output-directory",
                        "/operator/run",
                        "--private-work-directory",
                        "/operator/private",
                        "--image-size-evidence",
                        "/operator/image-evidence/linux-arm64.json",
                        "--record",
                        "/operator/run/execution-record.json",
                        "--skip-audit",
                    ]
                )
        self.assertEqual(2, raised.exception.code)


if __name__ == "__main__":
    unittest.main()
