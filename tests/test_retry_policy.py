# SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import unittest

from ossp_router.protocol import Decision, Submission
from ossp_router.runtime import (
    AttemptKind,
    AttemptResult,
    run_with_retry,
)


def _submission() -> Submission:
    return Submission(
        schema_version=1,
        challenge_id="phase-c-test",
        policy_id="ossp-2026-prompt-router-v1",
        split="synthetic",
        tier="fast",
        decisions=(Decision("opaque", "ax31-light"),),
    )


class RetryPolicyTest(unittest.TestCase):
    def test_first_valid_result_stops_additional_runs(self) -> None:
        calls = []
        results = iter(
            [
                AttemptResult(AttemptKind.EXECUTION_FAILURE, "bad JSON"),
                AttemptResult(
                    AttemptKind.VALID,
                    "valid",
                    submission=_submission(),
                ),
                AttemptResult(AttemptKind.EXECUTION_FAILURE, "must not run"),
            ]
        )

        def attempt(number):
            calls.append(number)
            return next(results)

        outcome = run_with_retry(attempt)
        self.assertEqual([1, 2], calls)
        self.assertEqual(2, outcome.official_attempts)
        self.assertFalse(outcome.tier_score_zero)
        self.assertIsNotNone(outcome.selected_submission)

    def test_three_execution_failures_make_only_the_tier_zero(self) -> None:
        calls = []

        def attempt(number):
            calls.append(number)
            return AttemptResult(AttemptKind.EXECUTION_FAILURE, "failed")

        outcome = run_with_retry(attempt)
        self.assertEqual([1, 2, 3], calls)
        self.assertEqual(3, outcome.official_attempts)
        self.assertTrue(outcome.tier_score_zero)
        self.assertFalse(outcome.disqualified)

    def test_operator_failure_does_not_consume_participant_attempt(self) -> None:
        results = iter(
            [
                AttemptResult(AttemptKind.INFRASTRUCTURE_FAILURE, "host failure"),
                AttemptResult(AttemptKind.EXECUTION_FAILURE, "participant failure"),
                AttemptResult(
                    AttemptKind.VALID,
                    "valid",
                    submission=_submission(),
                ),
            ]
        )
        numbers = []

        def attempt(number):
            numbers.append(number)
            return next(results)

        outcome = run_with_retry(attempt)
        self.assertEqual([1, 1, 2], numbers)
        self.assertEqual(2, outcome.official_attempts)
        self.assertEqual(1, outcome.infrastructure_failures)

    def test_repeated_operator_failure_is_not_scored_as_tier_zero(self) -> None:
        def attempt(_number):
            return AttemptResult(AttemptKind.INFRASTRUCTURE_FAILURE, "host failure")

        outcome = run_with_retry(attempt, max_infrastructure_failures=2)
        self.assertTrue(outcome.infrastructure_unavailable)
        self.assertFalse(outcome.tier_score_zero)
        self.assertFalse(outcome.disqualified)
        self.assertEqual(0, outcome.official_attempts)
        self.assertEqual(2, outcome.infrastructure_failures)
        self.assertEqual(2, len(outcome.history))

    def test_cleanup_pending_stops_operator_retries_immediately(self) -> None:
        calls = []

        def attempt(number):
            calls.append(number)
            return AttemptResult(
                AttemptKind.INFRASTRUCTURE_FAILURE,
                "Docker cleanup requires operator recovery",
                cleanup_pending=True,
            )

        outcome = run_with_retry(attempt)
        self.assertEqual([1], calls)
        self.assertTrue(outcome.infrastructure_unavailable)
        self.assertFalse(outcome.tier_score_zero)
        self.assertEqual(0, outcome.official_attempts)
        self.assertEqual(1, outcome.infrastructure_failures)

    def test_fairness_violation_disqualifies_whole_submission(self) -> None:
        outcome = run_with_retry(
            lambda _number: AttemptResult(
                AttemptKind.FAIRNESS_VIOLATION,
                "external inference",
            )
        )
        self.assertTrue(outcome.disqualified)
        self.assertFalse(outcome.tier_score_zero)
        self.assertEqual(0, outcome.official_attempts)

if __name__ == "__main__":
    unittest.main()
