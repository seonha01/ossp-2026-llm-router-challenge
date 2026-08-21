# SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
# SPDX-License-Identifier: Apache-2.0

"""Route with prompt features and a conservative batch-level cost budget."""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from ossp_router.heuristic import (
    complexity_score,
    episode_text,
    extract_features,
    write_submission_atomic,
)
from ossp_router.protocol import (
    TIERS,
    Decision,
    Episode,
    InputBatch,
    ProtocolError,
    RoutingPolicy,
    Submission,
    load_bundled_policy,
    load_input,
    load_policy,
    parse_submission,
    submission_to_dict,
)


SAFETY_RATIO = Decimal("0.85")
_FORMAL_REASONING = re.compile(
    r"\b(?:prove|derive|theorem|lemma|counterexample|induction|"
    r"증명|유도|정리|보조정리|반례|귀납)\b",
    re.IGNORECASE,
)
_PROGRAM_ANALYSIS = re.compile(
    r"```|\b(?:traceback|exception|complexity|big[- ]?o|"
    r"시간\s*복잡도|공간\s*복잡도|예외|스택\s*추적)\b",
    re.IGNORECASE,
)
_MULTI_CONSTRAINT = re.compile(
    r"\b(?:exactly|at least|at most|must|only|without|"
    r"정확히|이상|이하|반드시|오직|제외하고)\b",
    re.IGNORECASE,
)
_SIMPLE_TRANSFORM = re.compile(
    r"\b(?:summari[sz]e|rewrite|translate|list|extract|"
    r"요약|바꾸|번역|나열|추출)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class FeatureBudgetPlan:
    submission: Submission
    estimated_budget_ratio: Decimal
    safety_ratio: Decimal


def priority_score(episode: Episode) -> int:
    """Estimate difficulty from coarse semantic and structural markers."""

    features = extract_features(episode)
    text = episode_text(episode)
    score = complexity_score(features)
    if _FORMAL_REASONING.search(text):
        score += 2
    if _PROGRAM_ANALYSIS.search(text):
        score += 2
    if len(_MULTI_CONSTRAINT.findall(text)) >= 2:
        score += 1
    if (
        features.character_count < 500
        and score <= 2
        and _SIMPLE_TRANSFORM.search(text)
    ):
        score -= 1
    return max(0, score)


def _model_multipliers(policy: RoutingPolicy) -> Mapping[str, Decimal]:
    light = policy.models[policy.light_model_id]
    if any(model.fixed_cost != 0 for model in policy.models.values()):
        raise ProtocolError("이 baseline은 fixed_cost가 0인 정책만 지원합니다.")
    if light.input_token_rate <= 0 or light.output_token_rate <= 0:
        raise ProtocolError("경량 모델 토큰 단가는 0보다 커야 합니다.")
    return {
        model_id: max(
            rates.input_token_rate / light.input_token_rate,
            rates.output_token_rate / light.output_token_rate,
        )
        for model_id, rates in policy.models.items()
    }


def _content_weights(inputs: InputBatch) -> Tuple[Decimal, ...]:
    return tuple(
        Decimal(max(1, extract_features(episode).character_count))
        for episode in inputs.episodes
    )


def _upgrade_groups(
    *,
    priorities: Sequence[int],
    weights: Sequence[Decimal],
    selected: List[str],
    target_model: str,
    minimum_priority: int,
    multipliers: Mapping[str, Decimal],
    cost: Decimal,
    cap: Decimal,
) -> Decimal:
    """Upgrade whole equal-priority groups to preserve order invariance."""

    groups: Dict[int, List[int]] = {}
    for index, priority in enumerate(priorities):
        if priority >= minimum_priority:
            groups.setdefault(priority, []).append(index)
    for priority in sorted(groups, reverse=True):
        indexes = groups[priority]
        delta = sum(
            (
                weights[index]
                * (multipliers[target_model] - multipliers[selected[index]])
                for index in indexes
                if multipliers[target_model] > multipliers[selected[index]]
            ),
            Decimal("0"),
        )
        if delta > 0 and cost + delta <= cap:
            for index in indexes:
                if multipliers[target_model] > multipliers[selected[index]]:
                    selected[index] = target_model
            cost += delta
    return cost


def make_feature_budget_submission(
    inputs: InputBatch,
    policy: RoutingPolicy,
    tier: str,
) -> FeatureBudgetPlan:
    """Create a content-only submission under a conservative surrogate cap."""

    if inputs.schema_version != policy.schema_version:
        raise ProtocolError("입력과 정책의 schema_version이 일치하지 않습니다.")
    if tier not in TIERS:
        raise ProtocolError(f"알 수 없는 tier: {tier}")
    multipliers = _model_multipliers(policy)
    required_models = {policy.light_model_id, "ax31"}
    if not required_models.issubset(multipliers):
        raise ProtocolError("baseline에 필요한 모델 프로필이 정책에 없습니다.")

    priorities = tuple(priority_score(episode) for episode in inputs.episodes)
    weights = _content_weights(inputs)
    baseline = sum(weights, Decimal("0"))
    cap = baseline * policy.tiers[tier].budget_multiplier * SAFETY_RATIO
    selected = [policy.light_model_id for _episode in inputs.episodes]
    cost = baseline

    ax31_minimum = {"fast": 5, "balanced": 2, "premium": 0}[tier]
    cost = _upgrade_groups(
        priorities=priorities,
        weights=weights,
        selected=selected,
        target_model="ax31",
        minimum_priority=ax31_minimum,
        multipliers=multipliers,
        cost=cost,
        cap=cap,
    )
    submission = Submission(
        schema_version=inputs.schema_version,
        challenge_id=inputs.challenge_id,
        policy_id=policy.policy_id,
        split=inputs.split,
        tier=tier,
        decisions=tuple(
            Decision(episode.episode_id, model_id)
            for episode, model_id in zip(inputs.episodes, selected)
        ),
    )
    with localcontext() as context:
        context.prec = 80
        ratio = cost / baseline
    return FeatureBudgetPlan(
        submission=parse_submission(submission_to_dict(submission)),
        estimated_budget_ratio=ratio,
        safety_ratio=SAFETY_RATIO,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="프롬프트 특징과 보수적 비용 배분을 사용하는 baseline 라우터"
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--tier", choices=TIERS, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--policy", type=Path)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        inputs = load_input(args.input)
        policy = (
            load_policy(args.policy)
            if args.policy is not None
            else load_bundled_policy()
        )
        plan = make_feature_budget_submission(inputs, policy, args.tier)
        write_submission_atomic(args.output, plan.submission)
    except (OSError, ProtocolError, ValueError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2
    print(
        "OK: "
        f"{args.tier} 제출 파일을 생성했습니다 "
        f"(추정 비용 비율 {plan.estimated_budget_ratio:.6f})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
