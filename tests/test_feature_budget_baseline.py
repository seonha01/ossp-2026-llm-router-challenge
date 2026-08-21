# SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import importlib.util
import pathlib
import sys
import tempfile
import unittest

from ossp_router.heuristic import episode_text
from ossp_router.protocol import (
    load_bundled_policy,
    load_input,
    load_outcomes,
    parse_input,
)
from ossp_router.scoring import score_submissions


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "feature_budget_baseline",
    ROOT / "baselines/feature_budget.py",
)
assert SPEC is not None and SPEC.loader is not None
feature_budget = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = feature_budget
SPEC.loader.exec_module(feature_budget)


def _changed_batch(original, *, reverse=False):
    episodes = list(original.episodes)
    if reverse:
        episodes.reverse()
    return parse_input(
        {
            "schema_version": original.schema_version,
            "challenge_id": original.challenge_id,
            "split": original.split,
            "episodes": [
                {
                    "episode_id": f"opaque-{index}",
                    **(
                        {"prompt": episode.prompt}
                        if episode.prompt is not None
                        else {
                            "messages": [
                                {
                                    "role": message.role,
                                    "content": message.content,
                                }
                                for message in episode.messages or ()
                            ]
                        }
                    ),
                }
                for index, episode in enumerate(episodes)
            ],
        }
    )


def _by_content(inputs, submission):
    models = {
        decision.episode_id: decision.model_id
        for decision in submission.decisions
    }
    return {
        episode_text(episode): models[episode.episode_id]
        for episode in inputs.episodes
    }


class FeatureBudgetBaselineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.inputs = load_input(ROOT / "data/toy/inputs.json")
        self.outcomes = load_outcomes(ROOT / "data/toy/outcomes.json")
        self.policy = load_bundled_policy()

    def test_toy_score_improves_without_exceeding_any_budget(self) -> None:
        plans = [
            feature_budget.make_feature_budget_submission(
                self.inputs,
                self.policy,
                tier,
            )
            for tier in ("fast", "balanced", "premium")
        ]
        report = score_submissions(
            self.inputs,
            self.outcomes,
            [plan.submission for plan in plans],
            self.policy,
        )
        self.assertEqual("0.58", report["final_score"])
        self.assertTrue(
            all(item["budget_passed"] for item in report["tiers"].values())
        )
        self.assertEqual(
            {
                "fast": ["ax31-light", "ax31-light", "ax31-light"],
                "balanced": ["ax31", "ax31-light", "ax31-light"],
                "premium": ["ax31", "ax31", "ax31"],
            },
            {
                plan.submission.tier: [
                    decision.model_id
                    for decision in plan.submission.decisions
                ]
                for plan in plans
            },
        )

    def test_surrogate_ratio_stays_below_safety_cap(self) -> None:
        for tier in ("fast", "balanced", "premium"):
            with self.subTest(tier=tier):
                plan = feature_budget.make_feature_budget_submission(
                    self.inputs,
                    self.policy,
                    tier,
                )
                self.assertLessEqual(
                    plan.estimated_budget_ratio,
                    self.policy.tiers[tier].budget_multiplier
                    * plan.safety_ratio,
                )

    def test_ids_and_order_do_not_change_content_decisions(self) -> None:
        changed = _changed_batch(self.inputs, reverse=True)
        for tier in ("fast", "balanced", "premium"):
            with self.subTest(tier=tier):
                original = feature_budget.make_feature_budget_submission(
                    self.inputs,
                    self.policy,
                    tier,
                ).submission
                reordered = feature_budget.make_feature_budget_submission(
                    changed,
                    self.policy,
                    tier,
                ).submission
                self.assertEqual(
                    _by_content(self.inputs, original),
                    _by_content(changed, reordered),
                )

    def test_cli_writes_one_valid_submission(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = pathlib.Path(temporary) / "balanced.json"
            result = feature_budget.main(
                [
                    "--input",
                    str(ROOT / "data/toy/inputs.json"),
                    "--tier",
                    "balanced",
                    "--output",
                    str(output),
                ]
            )
            self.assertEqual(0, result)
            self.assertTrue(output.is_file())


if __name__ == "__main__":
    unittest.main()
