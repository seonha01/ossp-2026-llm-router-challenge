# SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

from ossp_router.orchestrator import ResolvedImage
from ossp_router.protocol import (
    Decision,
    Episode,
    InputBatch,
    Submission,
    load_bundled_policy,
    write_json,
)
from ossp_router.runtime import (
    AttemptKind,
    AttemptResult,
    ImageSizeEvidence,
    InfrastructureUnavailable,
    PHASE_C_CANDIDATE_LIMITS,
    run_router_once,
)
from ossp_router.tiebreak_latency import (
    LatencyCandidate,
    _comparison_time_ns,
    _median_of_five,
    _rank_candidates,
    load_latency_candidates,
    measure_tiebreak_latency,
)


def _inputs() -> InputBatch:
    return InputBatch(
        schema_version=1,
        challenge_id="tiebreak-test",
        split="synthetic",
        episodes=(Episode("opaque", prompt="PRIVATE-PROMPT-SENTINEL"),),
    )


def _submission(inputs: InputBatch, tier: str) -> Submission:
    return Submission(
        schema_version=inputs.schema_version,
        challenge_id=inputs.challenge_id,
        policy_id=load_bundled_policy().policy_id,
        split=inputs.split,
        tier=tier,
        decisions=(Decision("opaque", "ax31-light"),),
    )


def _evidence(letter: str) -> ImageSizeEvidence:
    return ImageSizeEvidence(
        submitted_digest=(
            f"registry.example/challenge/router-{letter}@sha256:" + letter * 64
        ),
        selected_manifest_digest="sha256:" + letter * 64,
        config_digest="sha256:" + ("f" if letter != "f" else "e") * 64,
        operating_system="linux",
        architecture="arm64",
        oci_compressed_layer_bytes=1000,
        rootfs_apparent_bytes=2000,
        oci_layer_measurement_method="oci-manifest-layer-descriptors-v1",
        rootfs_measurement_method="docker-export-tar-apparent-size-v1",
        evidence_sha256=("e" if letter != "e" else "d") * 64,
    )


def _candidate(identifier: str, letter: str) -> LatencyCandidate:
    evidence = _evidence(letter)
    return LatencyCandidate(
        submission_id=identifier,
        commit_sha=letter * 40,
        submitted_image_digest=evidence.submitted_digest,
        image_size_evidence=evidence,
    )


def _resolver(_runtime, submitted, evidence) -> ResolvedImage:
    assert evidence is not None
    return ResolvedImage(
        submitted_digest=submitted,
        local_image_id=evidence.config_digest,
        operating_system="linux",
        architecture="arm64",
        selected_manifest_digest=evidence.selected_manifest_digest,
        oci_compressed_layer_bytes=evidence.oci_compressed_layer_bytes,
        rootfs_apparent_bytes=evidence.rootfs_apparent_bytes,
        size_evidence_sha256=evidence.evidence_sha256,
        execution_image_reference=submitted,
    )


class TiebreakLatencyTest(unittest.TestCase):
    def _measure(self, candidates, runner, *, timer_resolution_ns=1):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            input_path = root / "input.json"
            output_directory = root / "output"
            private_directory = root / "private"
            private_directory.mkdir(mode=0o700)
            record_path = output_directory / "latency.json"
            inputs = _inputs()
            write_json(
                input_path,
                {
                    "schema_version": 1,
                    "challenge_id": inputs.challenge_id,
                    "split": inputs.split,
                    "episodes": [
                        {
                            "episode_id": "opaque",
                            "prompt": "PRIVATE-PROMPT-SENTINEL",
                        }
                    ],
                },
            )
            with mock.patch(
                "ossp_router.tiebreak_latency.require_no_pending_runtime_resources"
            ):
                report = measure_tiebreak_latency(
                    runtime_command=("docker",),
                    candidates=candidates,
                    inputs=inputs,
                    policy=load_bundled_policy(),
                    input_path=input_path,
                    output_directory=output_directory,
                    private_work_directory=private_directory,
                    record_path=record_path,
                    timer_resolution_ns=timer_resolution_ns,
                    image_resolver=_resolver,
                    container_runner=runner,
                )
            on_disk = json.loads(record_path.read_text(encoding="utf-8"))
            return report, on_disk

    def test_alternates_candidate_order_and_ranks_sum_of_tier_medians(self) -> None:
        candidates = (
            _candidate("candidate-a", "a"),
            _candidate("candidate-b", "b"),
            _candidate("candidate-c", "c"),
        )
        calls = []
        elapsed = {"a": 10, "b": 30, "c": 20}

        def runner(_runtime, **kwargs):
            letter = kwargs["image"].split("router-")[1][0]
            calls.append((kwargs["tier"], letter))
            return AttemptResult(
                AttemptKind.VALID,
                "valid",
                submission=_submission(_inputs(), kwargs["tier"]),
                measurement_elapsed_ns=elapsed[letter],
            )

        report, on_disk = self._measure(candidates, runner)
        fast_calls = [letter for tier, letter in calls if tier == "fast"]
        self.assertEqual(
            [
                "a", "b", "c",  # one excluded warmup per candidate
                "a", "b", "c",  # repetition 1
                "b", "c", "a",  # repetition 2
                "c", "a", "b",  # repetition 3
                "a", "b", "c",  # repetition 4
                "b", "c", "a",  # repetition 5
            ],
            fast_calls,
        )
        self.assertEqual(
            ["candidate-a", "candidate-c", "candidate-b"],
            [row["submission_id"] for row in report["ranking"]],
        )
        self.assertEqual([1, 2, 3], [row["rank"] for row in report["ranking"]])
        self.assertEqual(report, on_disk)
        serialized = json.dumps(on_disk, ensure_ascii=False)
        self.assertNotIn("PRIVATE-PROMPT-SENTINEL", serialized)
        self.assertNotIn("input_sha256", serialized)
        self.assertNotIn("output_sha256", serialized)
        self.assertNotIn("detail", serialized)

    def test_participant_failure_receives_tier_time_limit(self) -> None:
        candidates = (
            _candidate("valid", "a"),
            _candidate("failed", "b"),
        )

        def runner(_runtime, **kwargs):
            if "router-b" in kwargs["image"]:
                return AttemptResult(AttemptKind.EXECUTION_FAILURE, "secret reason")
            return AttemptResult(
                AttemptKind.VALID,
                "valid",
                submission=_submission(_inputs(), kwargs["tier"]),
                measurement_elapsed_ns=50,
            )

        report, _ = self._measure(candidates, runner)
        failed = next(
            item for item in report["candidates"]
            if item["submission_id"] == "failed"
        )
        expected = PHASE_C_CANDIDATE_LIMITS.wall_time_seconds * 1_000_000_000
        for tier in ("fast", "balanced", "premium"):
            self.assertEqual(expected, failed["tiers"][tier]["median_ns"])
            self.assertTrue(
                all(
                    item["result"] == "participant_failure_limit_assigned"
                    for item in failed["tiers"][tier]["measurements"]
                )
            )

    def test_operator_failure_is_discarded_and_same_condition_repeated(self) -> None:
        candidates = (_candidate("candidate-a", "a"), _candidate("candidate-b", "b"))
        failed_once = False

        def runner(_runtime, **kwargs):
            nonlocal failed_once
            if not failed_once and kwargs["tier"] == "fast":
                failed_once = True
                return AttemptResult(AttemptKind.INFRASTRUCTURE_FAILURE, "host")
            return AttemptResult(
                AttemptKind.VALID,
                "valid",
                submission=_submission(_inputs(), kwargs["tier"]),
                measurement_elapsed_ns=100,
            )

        report, _ = self._measure(candidates, runner)
        first = report["candidates"][0]["tiers"]["fast"]
        self.assertEqual(1, first["discarded_infrastructure_attempts"])
        self.assertEqual(5, len(first["measurements"]))

    def test_repeated_operator_failure_aborts_without_publishing_record(self) -> None:
        candidates = (_candidate("candidate-a", "a"), _candidate("candidate-b", "b"))
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            input_path = root / "input.json"
            write_json(
                input_path,
                {
                    "schema_version": 1,
                    "challenge_id": "tiebreak-test",
                    "split": "synthetic",
                    "episodes": [{"episode_id": "opaque", "prompt": "p"}],
                },
            )
            inputs = InputBatch(
                1,
                "tiebreak-test",
                "synthetic",
                (Episode("opaque", prompt="p"),),
            )
            private = root / "private"
            private.mkdir(mode=0o700)
            record = root / "output" / "latency.json"
            with mock.patch(
                "ossp_router.tiebreak_latency.require_no_pending_runtime_resources"
            ):
                with self.assertRaises(InfrastructureUnavailable):
                    measure_tiebreak_latency(
                        runtime_command=("docker",),
                        candidates=candidates,
                        inputs=inputs,
                        policy=load_bundled_policy(),
                        input_path=input_path,
                        output_directory=record.parent,
                        private_work_directory=private,
                        record_path=record,
                        image_resolver=_resolver,
                        container_runner=lambda *_args, **_kwargs: AttemptResult(
                            AttemptKind.INFRASTRUCTURE_FAILURE,
                            "host",
                        ),
                    )
            self.assertFalse(record.exists())

    def test_valid_result_without_timer_is_operator_failure(self) -> None:
        candidates = (_candidate("candidate-a", "a"), _candidate("candidate-b", "b"))

        def runner(_runtime, **kwargs):
            return AttemptResult(
                AttemptKind.VALID,
                "valid",
                submission=_submission(_inputs(), kwargs["tier"]),
            )

        with self.assertRaises(InfrastructureUnavailable):
            self._measure(candidates, runner)

    def test_rank_jointly_at_timer_precision(self) -> None:
        rows = _rank_candidates(
            {"a": 102, "b": 104, "c": 111},
            timer_resolution_ns=10,
        )
        self.assertEqual([1, 1, 3], [row["rank"] for row in rows])
        self.assertEqual([100, 100, 110], [row["comparison_time_ns"] for row in rows])
        self.assertEqual(100, _comparison_time_ns(104, 10))

    def test_median_requires_five_nonnegative_integer_samples(self) -> None:
        self.assertEqual(3, _median_of_five([5, 1, 3, 2, 4]))
        with self.assertRaises(ValueError):
            _median_of_five([1, 2, 3])
        with self.assertRaises(ValueError):
            _median_of_five([1, 2, -3, 4, 5])

    def test_candidate_loader_rejects_duplicate_keys_before_evidence_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = pathlib.Path(temporary) / "candidates.json"
            path.write_text(
                '{"schema_version":1,"schema_version":1,"candidates":[]}',
                encoding="utf-8",
            )
            with mock.patch(
                "ossp_router.tiebreak_latency.load_image_size_evidence"
            ) as loader:
                with self.assertRaisesRegex(ValueError, "중복 JSON 키"):
                    load_latency_candidates(path)
                loader.assert_not_called()

    def test_runtime_populates_measurement_through_submission_validation(self) -> None:
        inputs = _inputs()
        submission = {
            "schema_version": 1,
            "challenge_id": inputs.challenge_id,
            "policy_id": load_bundled_policy().policy_id,
            "split": inputs.split,
            "tier": "fast",
            "decisions": [
                {"episode_id": "opaque", "model_id": "ax31-light"}
            ],
        }
        script = (
            "import argparse,json;"
            "p=argparse.ArgumentParser();"
            "p.add_argument('--input');p.add_argument('--tier');"
            "p.add_argument('--output');a=p.parse_args();"
            f"json.dump({submission!r},open(a.output,'w'))"
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            input_path = root / "input.json"
            output_path = root / "submission.json"
            input_path.write_text("{}", encoding="utf-8")
            result = run_router_once(
                (sys.executable, "-c", script),
                inputs=inputs,
                policy=load_bundled_policy(),
                input_path=input_path,
                output_path=output_path,
                tier="fast",
            )
        self.assertIs(result.kind, AttemptKind.VALID)
        self.assertIsInstance(result.measurement_elapsed_ns, int)
        self.assertGreater(result.measurement_elapsed_ns or 0, 0)


if __name__ == "__main__":
    unittest.main()
