# SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest

from ossp_router.heuristic import episode_text
from ossp_router.protocol import (
    ProtocolError,
    load_bundled_policy,
    load_input,
    load_outcomes,
    parse_input,
)
from ossp_router.scoring import score_submissions


ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


hash_regex = _load_module("hash_regex", ROOT / "baselines/hash_regex.py")
train_hash_regex = _load_module(
    "train_hash_regex", ROOT / "baselines/train_hash_regex.py"
)


def _changed_batch(original):
    return parse_input(
        {
            "schema_version": original.schema_version,
            "challenge_id": original.challenge_id,
            "split": original.split,
            "episodes": [
                {
                    "episode_id": f"changed-{index}",
                    **(
                        {"prompt": episode.prompt}
                        if episode.prompt is not None
                        else {
                            "messages": [
                                {"role": item.role, "content": item.content}
                                for item in episode.messages or ()
                            ]
                        }
                    ),
                }
                for index, episode in enumerate(reversed(original.episodes))
            ],
        }
    )


def _by_content(inputs, submission):
    decisions = {
        decision.episode_id: decision.model_id
        for decision in submission.decisions
    }
    return {
        episode_text(episode): decisions[episode.episode_id]
        for episode in inputs.episodes
    }


@unittest.skipUnless(train_hash_regex.np is not None, "NumPy가 설치되지 않음")
class HashRegexBaselineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.target = pathlib.Path(cls.temporary.name)
        cls.artifact_path = cls.target / "artifact.json"
        cls.report_path = cls.target / "report.json"
        cls.inputs = load_input(ROOT / "data/toy/inputs.json")
        cls.outcomes = load_outcomes(ROOT / "data/toy/outcomes.json")
        cls.policy = load_bundled_policy()
        cls.report = train_hash_regex.train(
            input_path=ROOT / "data/toy/inputs.json",
            outcomes_path=ROOT / "data/toy/outcomes.json",
            artifact_path=cls.artifact_path,
            report_path=cls.report_path,
            policy=cls.policy,
            hash_bins=16,
            requested_folds=3,
            alpha_candidates=(0.1, 1.0, 10.0),
            safety_grid_size=11,
        )
        cls.artifact = hash_regex.load_artifact(cls.artifact_path)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def test_training_artifact_contains_only_global_parameters(self) -> None:
        raw = json.loads(self.artifact_path.read_text(encoding="utf-8"))
        serialized = json.dumps(raw, ensure_ascii=False, sort_keys=True)
        for episode in self.inputs.episodes:
            self.assertNotIn(episode.episode_id, serialized)
            self.assertNotIn(episode_text(episode), serialized)
        self.assertEqual(
            "numpy-ridge-oof-grid-v1",
            raw["training_summary"]["optimizer"],
        )
        self.assertEqual(3, raw["training_summary"]["num_episodes"])
        self.assertEqual(30, self.report["feature_dimension"])

    def test_training_is_byte_deterministic(self) -> None:
        artifact = self.target / "artifact-second.json"
        report = self.target / "report-second.json"
        train_hash_regex.train(
            input_path=ROOT / "data/toy/inputs.json",
            outcomes_path=ROOT / "data/toy/outcomes.json",
            artifact_path=artifact,
            report_path=report,
            policy=self.policy,
            hash_bins=16,
            requested_folds=3,
            alpha_candidates=(0.1, 1.0, 10.0),
            safety_grid_size=11,
        )
        self.assertEqual(self.artifact_path.read_bytes(), artifact.read_bytes())
        self.assertEqual(self.report_path.read_bytes(), report.read_bytes())

    def test_toy_plumbing_scores_and_passes_all_budgets(self) -> None:
        submissions = [
            hash_regex.make_hash_regex_submission(
                self.inputs, self.policy, self.artifact, tier
            ).submission
            for tier in ("fast", "balanced", "premium")
        ]
        report = score_submissions(
            self.inputs,
            self.outcomes,
            submissions,
            self.policy,
        )
        self.assertGreaterEqual(float(report["final_score"]), 0.5)
        self.assertTrue(
            all(item["budget_passed"] for item in report["tiers"].values())
        )
        self.assertEqual(
            self.report["fitted_train_self_check"]["final_score"],
            report["final_score"],
        )

    def test_ids_and_order_do_not_change_content_decisions(self) -> None:
        changed = _changed_batch(self.inputs)
        for tier in ("fast", "balanced", "premium"):
            with self.subTest(tier=tier):
                original = hash_regex.make_hash_regex_submission(
                    self.inputs, self.policy, self.artifact, tier
                ).submission
                reordered = hash_regex.make_hash_regex_submission(
                    changed, self.policy, self.artifact, tier
                ).submission
                self.assertEqual(
                    _by_content(self.inputs, original),
                    _by_content(changed, reordered),
                )

    def test_ax31_fill_keeps_existing_non_light_choices(self) -> None:
        scores = (
            {"ax31-light": 0.1, "ax31": 0.8, "axk1-think": 0.9},
            {"ax31-light": 0.1, "ax31": 0.8, "axk1-think": 0.9},
            {"ax31-light": 0.1, "ax31": 0.8, "axk1-think": 0.9},
        )
        costs = (
            {"ax31-light": 1.0, "ax31": 2.0, "axk1-think": 3.0},
            {"ax31-light": 1.0, "ax31": 2.0, "axk1-think": 3.0},
            {"ax31-light": 1.0, "ax31": 2.0, "axk1-think": 3.0},
        )
        selected, ratio = hash_regex.fill_ax31_upgrades(
            ("ax31-light", "ax31", "axk1-think"),
            scores,
            costs,
            budget_multiplier=4.0,
            safety_ratio=0.65,
        )
        self.assertEqual(("ax31", "ax31", "axk1-think"), selected)
        self.assertEqual(7.0 / 3.0, ratio)

    def test_only_premium_uses_ax31_fill(self) -> None:
        for tier in ("fast", "balanced", "premium"):
            with self.subTest(tier=tier):
                plan = hash_regex.make_hash_regex_submission(
                    self.inputs, self.policy, self.artifact, tier
                )
                self.assertEqual(
                    hash_regex.PREMIUM_AX31_FILL_SAFETY_RATIO
                    if tier == "premium"
                    else None,
                    plan.ax31_fill_safety_ratio,
                )

    def test_artifact_parser_rejects_undeclared_fields(self) -> None:
        raw = json.loads(self.artifact_path.read_text(encoding="utf-8"))
        raw["undeclared_extension"] = {"some-key": "some-value"}
        with self.assertRaises(ProtocolError):
            hash_regex.parse_artifact(raw)

    def test_public_artifact_and_dev_report_match_released_data(self) -> None:
        artifact_path = ROOT / "baselines/hash-regex-public.v1.json"
        report_path = ROOT / "baselines/hash-regex-public-dev-report.v1.json"
        artifact = hash_regex.load_artifact(artifact_path)
        public_data = json.loads(
            (ROOT / "data/public-data.v1.json").read_text(encoding="utf-8")
        )
        summary = artifact.training_summary
        self.assertEqual(
            public_data["splits"]["train"]["sha256"]["materialized_inputs"],
            summary["input_sha256"],
        )
        self.assertEqual(
            public_data["splits"]["train"]["sha256"]["outcomes"],
            summary["outcomes_sha256"],
        )
        self.assertEqual(
            public_data["splits"]["dev"]["sha256"]["materialized_inputs"],
            summary["validation_input_sha256"],
        )
        self.assertEqual(
            public_data["splits"]["dev"]["sha256"]["outcomes"],
            summary["validation_outcomes_sha256"],
        )
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual("0.695369318182", report["final_score"])
        self.assertTrue(
            all(item["budget_passed"] for item in report["tiers"].values())
        )


if __name__ == "__main__":
    unittest.main()
