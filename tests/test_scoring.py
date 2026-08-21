# SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pathlib
import unittest
from dataclasses import replace
from decimal import Decimal

from ossp_router.protocol import (
    TIERS,
    Decision,
    ModelRates,
    Submission,
    TierPolicy,
    load_input,
    load_outcomes,
    load_policy,
)
from ossp_router.scoring import ScoringError, _score_text, score_submissions


ROOT = pathlib.Path(__file__).resolve().parents[1]
POLICY_SHA256 = "7c892c423da5fa762e7e1a93b9fa071be51e259b65d2b63a5ba434c4342d7a8e"


class ScoringTest(unittest.TestCase):
    def setUp(self) -> None:
        self.inputs = load_input(ROOT / "data/toy/inputs.json")
        self.outcomes = load_outcomes(ROOT / "data/toy/outcomes.json")
        self.policy = load_policy(ROOT / "configs/routing-policy.v1.json")

    def submissions(self, model_id: str = "ax31-light"):
        decisions = tuple(
            Decision(episode.episode_id, model_id) for episode in self.inputs.episodes
        )
        return [
            Submission(
                self.inputs.schema_version,
                self.inputs.challenge_id,
                self.policy.policy_id,
                self.inputs.split,
                tier,
                decisions,
            )
            for tier in TIERS
        ]

    def test_all_light_reference_score_and_audit_fields(self) -> None:
        report = score_submissions(
            self.inputs, self.outcomes, self.submissions(), self.policy
        )
        self.assertEqual("0.5", report["final_score"])
        self.assertEqual(POLICY_SHA256, report["policy_sha256"])
        self.assertEqual(12, report["score_decimal_places"])
        self.assertEqual("ROUND_HALF_EVEN", report["score_rounding"])
        for tier in TIERS:
            tier_report = report["tiers"][tier]
            self.assertEqual("1", tier_report["budget_ratio"])
            self.assertEqual("0.5", tier_report["tier_score"])
            self.assertTrue(tier_report["budget_passed"])
            self.assertEqual(
                {"ax31-light": 3, "ax31": 0, "axk1-think": 0},
                tier_report["model_counts"],
            )
            self.assertEqual(300, tier_report["selected_input_tokens"])
            self.assertEqual(60, tier_report["selected_output_tokens"])
            self.assertEqual(3, tier_report["selected_generations"])
            self.assertEqual("credits", tier_report["cost_unit"])
            self.assertEqual(1_000_000, tier_report["token_unit"])
            self.assertIn("budget_limit", tier_report)

    def test_exact_budget_boundary_passes_and_any_excess_fails(self) -> None:
        models = dict(self.policy.models)
        models["ax31-light"] = ModelRates(
            Decimal("0"), Decimal("1"), Decimal("0")
        )
        models["ax31"] = ModelRates(
            Decimal("0"), Decimal("1.25"), Decimal("0")
        )
        boundary_policy = replace(self.policy, models=models)
        submissions = self.submissions()
        submissions[0] = replace(
            submissions[0],
            decisions=tuple(
                Decision(episode.episode_id, "ax31")
                for episode in self.inputs.episodes
            ),
        )
        report = score_submissions(
            self.inputs, self.outcomes, submissions, boundary_policy
        )
        self.assertTrue(report["tiers"]["fast"]["budget_passed"])

        tiers = dict(boundary_policy.tiers)
        tiers["fast"] = TierPolicy(
            Decimal("1.249999999999999"), Decimal("0.4")
        )
        strict_policy = replace(boundary_policy, tiers=tiers)
        report = score_submissions(
            self.inputs, self.outcomes, submissions, strict_policy
        )
        self.assertFalse(report["tiers"]["fast"]["budget_passed"])
        self.assertEqual("2.1", report["tiers"]["fast"]["quality_points_total"])
        self.assertEqual("0", report["tiers"]["fast"]["tier_points_total"])
        self.assertEqual("0", report["tiers"]["fast"]["tier_score"])
        self.assertEqual("0.9", report["final_weighted_points_total"])
        self.assertEqual("0.3", report["final_score"])

    def test_one_third_is_quantized_to_twelve_places(self) -> None:
        changed = []
        for outcome in self.outcomes.outcomes:
            score = (
                Decimal("1")
                if outcome.model_id == "ax31-light"
                and outcome.episode_id == self.inputs.episodes[0].episode_id
                else Decimal("0")
                if outcome.model_id == "ax31-light"
                else outcome.score
            )
            changed.append(replace(outcome, score=score))
        outcomes = replace(self.outcomes, outcomes=tuple(changed))
        report = score_submissions(
            self.inputs, outcomes, self.submissions(), self.policy
        )
        self.assertEqual("0.333333333333", report["final_score"])
        self.assertEqual(
            "1", report["tiers"]["fast"]["quality_points_total"]
        )

    def test_score_rounding_is_half_even(self) -> None:
        self.assertEqual("0.123456789012", _score_text(Decimal("0.1234567890125")))
        self.assertEqual("0.123456789014", _score_text(Decimal("0.1234567890135")))

    def test_input_and_outcome_order_do_not_change_report(self) -> None:
        forward = score_submissions(
            self.inputs, self.outcomes, self.submissions(), self.policy
        )
        reversed_inputs = replace(
            self.inputs, episodes=tuple(reversed(self.inputs.episodes))
        )
        reversed_outcomes = replace(
            self.outcomes, outcomes=tuple(reversed(self.outcomes.outcomes))
        )
        reverse = score_submissions(
            reversed_inputs,
            reversed_outcomes,
            list(reversed(self.submissions())),
            self.policy,
        )
        self.assertEqual(forward, reverse)

    def test_requires_complete_outcome_matrix(self) -> None:
        incomplete = replace(self.outcomes, outcomes=self.outcomes.outcomes[:-1])
        with self.assertRaises(ScoringError):
            score_submissions(
                self.inputs, incomplete, self.submissions(), self.policy
            )

    def test_requires_exact_submission_coverage(self) -> None:
        submissions = self.submissions()
        submissions[0] = replace(
            submissions[0], decisions=submissions[0].decisions[:-1]
        )
        with self.assertRaises(ScoringError):
            score_submissions(self.inputs, self.outcomes, submissions, self.policy)


if __name__ == "__main__":
    unittest.main()
