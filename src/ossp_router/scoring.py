# SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
# SPDX-License-Identifier: Apache-2.0

"""Deterministic v1 budget and quality scoring for router submissions."""

from __future__ import annotations

from collections import Counter
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import Any, Dict, Mapping, Sequence, Tuple

from .protocol import (
    SCORE_DECIMAL_PLACES,
    SCORE_ROUNDING,
    TIERS,
    InputBatch,
    Outcome,
    OutcomeBatch,
    RoutingPolicy,
    Submission,
    policy_sha256,
)


SCORING_PRECISION = 160


class ScoringError(ValueError):
    """Raised when otherwise valid files cannot be scored together."""


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _score_text(value: Decimal) -> str:
    quantum = Decimal(1).scaleb(-SCORE_DECIMAL_PLACES)
    with localcontext() as context:
        context.prec = SCORING_PRECISION
        context.rounding = ROUND_HALF_EVEN
        return _decimal_text(value.quantize(quantum))


def _cost(outcome: Outcome, policy: RoutingPolicy) -> Decimal:
    rates = policy.models[outcome.model_id]
    unit = Decimal(policy.token_unit)
    return (
        rates.fixed_cost
        + Decimal(outcome.input_tokens) * rates.input_token_rate / unit
        + Decimal(outcome.output_tokens) * rates.output_token_rate / unit
    )


def _validate_shared_metadata(
    inputs: InputBatch,
    outcomes: OutcomeBatch,
    submissions: Sequence[Submission],
    policy: RoutingPolicy,
) -> None:
    if inputs.schema_version != policy.schema_version:
        raise ScoringError("입력과 정책의 schema_version이 다릅니다.")
    if outcomes.schema_version != inputs.schema_version:
        raise ScoringError("outcomes와 입력의 schema_version이 다릅니다.")
    if outcomes.challenge_id != inputs.challenge_id:
        raise ScoringError("outcomes와 입력의 challenge_id가 다릅니다.")
    if outcomes.split != inputs.split:
        raise ScoringError("outcomes와 입력의 split이 다릅니다.")
    for submission in submissions:
        if submission.schema_version != inputs.schema_version:
            raise ScoringError("submission과 입력의 schema_version이 다릅니다.")
        if submission.challenge_id != inputs.challenge_id:
            raise ScoringError("submission과 입력의 challenge_id가 다릅니다.")
        if submission.policy_id != policy.policy_id:
            raise ScoringError("submission과 정책의 policy_id가 다릅니다.")
        if submission.split != inputs.split:
            raise ScoringError("submission과 입력의 split이 다릅니다.")


def _outcome_index(
    inputs: InputBatch, outcomes: OutcomeBatch, policy: RoutingPolicy
) -> Mapping[Tuple[str, str], Outcome]:
    input_ids = {episode.episode_id for episode in inputs.episodes}
    expected = {
        (episode_id, model_id)
        for episode_id in input_ids
        for model_id in policy.models
    }
    actual = {(item.episode_id, item.model_id) for item in outcomes.outcomes}
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing or extra:
        raise ScoringError(
            f"outcome 행렬이 완전하지 않습니다: 누락={missing}, 초과={extra}"
        )
    return {(item.episode_id, item.model_id): item for item in outcomes.outcomes}


def _decision_index(
    inputs: InputBatch, submission: Submission
) -> Mapping[str, str]:
    expected = {episode.episode_id for episode in inputs.episodes}
    decisions = {
        decision.episode_id: decision.model_id for decision in submission.decisions
    }
    missing = sorted(expected - set(decisions))
    extra = sorted(set(decisions) - expected)
    if missing or extra:
        raise ScoringError(
            f"{submission.tier} decision 범위 오류: 누락={missing}, 초과={extra}"
        )
    return decisions


def _score_tier(
    *,
    inputs: InputBatch,
    submission: Submission,
    outcome_by_key: Mapping[Tuple[str, str], Outcome],
    policy: RoutingPolicy,
    policy_digest: str,
) -> Dict[str, Any]:
    decisions = _decision_index(inputs, submission)
    model_counts = Counter(decisions.values())
    with localcontext() as context:
        context.prec = SCORING_PRECISION
        context.rounding = ROUND_HALF_EVEN
        total_cost = Decimal("0")
        light_baseline_cost = Decimal("0")
        quality_total = Decimal("0")
        selected_input_tokens = 0
        selected_output_tokens = 0
        selected_generations = 0
        for episode in inputs.episodes:
            selected = outcome_by_key[
                (episode.episode_id, decisions[episode.episode_id])
            ]
            light = outcome_by_key[
                (episode.episode_id, policy.light_model_id)
            ]
            total_cost += _cost(selected, policy)
            light_baseline_cost += _cost(light, policy)
            quality_total += selected.score
            selected_input_tokens += selected.input_tokens
            selected_output_tokens += selected.output_tokens
            selected_generations += selected.num_generations
        if light_baseline_cost <= 0:
            raise ScoringError("all-light 기준 비용은 0보다 커야 합니다.")
        tier_policy = policy.tiers[submission.tier]
        budget_limit = light_baseline_cost * tier_policy.budget_multiplier
        budget_ratio = total_cost / light_baseline_cost
        budget_passed = total_cost <= budget_limit
        quality_score = quality_total / Decimal(len(inputs.episodes))
        tier_score = quality_score if budget_passed else Decimal("0")
        near_budget = (
            budget_ratio
            >= tier_policy.budget_multiplier * policy.budget_warning_ratio
        )
    return {
        "schema_version": inputs.schema_version,
        "report_type": "prompt-only-routing-tier-score",
        "challenge_id": inputs.challenge_id,
        "policy_id": policy.policy_id,
        "policy_sha256": policy_digest,
        "split": inputs.split,
        "tier": submission.tier,
        "num_episodes": len(inputs.episodes),
        "model_counts": {
            model_id: model_counts.get(model_id, 0) for model_id in policy.models
        },
        "selected_generations": selected_generations,
        "selected_input_tokens": selected_input_tokens,
        "selected_output_tokens": selected_output_tokens,
        "cost_unit": policy.cost_unit,
        "token_unit": policy.token_unit,
        "light_baseline_cost": _decimal_text(light_baseline_cost),
        "budget_multiplier": _decimal_text(tier_policy.budget_multiplier),
        "budget_limit": _decimal_text(budget_limit),
        "total_cost": _decimal_text(total_cost),
        "budget_ratio": _score_text(budget_ratio),
        "budget_passed": budget_passed,
        "near_budget": near_budget,
        "score_decimal_places": SCORE_DECIMAL_PLACES,
        "score_rounding": SCORE_ROUNDING,
        "quality_points_total": _decimal_text(quality_total),
        "quality_score": _score_text(quality_score),
        "tier_points_total": _decimal_text(
            quality_total if budget_passed else Decimal("0")
        ),
        "tier_score": _score_text(tier_score),
        "over_budget_zero_applied": not budget_passed,
    }


def score_submissions(
    inputs: InputBatch,
    outcomes: OutcomeBatch,
    submissions: Sequence[Submission],
    policy: RoutingPolicy,
) -> Dict[str, Any]:
    """Score exactly one v1 submission for each tier."""

    by_tier = {submission.tier: submission for submission in submissions}
    if len(by_tier) != len(submissions) or set(by_tier) != set(TIERS):
        raise ScoringError(
            f"submission은 tier별로 정확히 하나씩 필요합니다: {list(TIERS)}"
        )
    _validate_shared_metadata(inputs, outcomes, submissions, policy)
    outcome_by_key = _outcome_index(inputs, outcomes, policy)
    digest = policy_sha256(policy)
    tier_reports = {
        tier: _score_tier(
            inputs=inputs,
            submission=by_tier[tier],
            outcome_by_key=outcome_by_key,
            policy=policy,
            policy_digest=digest,
        )
        for tier in TIERS
    }
    with localcontext() as context:
        context.prec = SCORING_PRECISION
        context.rounding = ROUND_HALF_EVEN
        final_weighted_points = sum(
            (
                Decimal(tier_reports[tier]["tier_points_total"])
                * policy.tiers[tier].weight
                for tier in TIERS
            ),
            Decimal("0"),
        )
        final_score = final_weighted_points / Decimal(len(inputs.episodes))
    return {
        "schema_version": inputs.schema_version,
        "report_type": "prompt-only-routing-final-score",
        "challenge_id": inputs.challenge_id,
        "policy_id": policy.policy_id,
        "policy_sha256": digest,
        "split": inputs.split,
        "tiers": tier_reports,
        "tier_weights": {
            tier: _decimal_text(policy.tiers[tier].weight) for tier in sorted(TIERS)
        },
        "score_decimal_places": SCORE_DECIMAL_PLACES,
        "score_rounding": SCORE_ROUNDING,
        "final_weighted_points_total": _decimal_text(final_weighted_points),
        "final_score": _score_text(final_score),
    }
