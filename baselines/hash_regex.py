# SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
# SPDX-License-Identifier: Apache-2.0

"""Route with regex features and a trained signed feature-hashing model."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Tuple

from ossp_router.heuristic import (
    episode_text,
    extract_features,
    write_submission_atomic,
)
from ossp_router.protocol import (
    MODEL_IDS,
    TIERS,
    Decision,
    Episode,
    InputBatch,
    ProtocolError,
    RoutingPolicy,
    Submission,
    load_bundled_policy,
    load_input,
    load_json,
    load_policy,
    parse_submission,
    policy_sha256,
    submission_to_dict,
)


ARTIFACT_TYPE = "ossp-hash-regex-linear-v1"
FEATURE_VERSION = 1
DEFAULT_HASH_BINS = 256
MIN_HASH_BINS = 16
MAX_HASH_BINS = 16_384
PREMIUM_AX31_FILL_SAFETY_RATIO = 0.65
_FNV_OFFSET = 14_695_981_039_346_656_037
_FNV_PRIME = 1_099_511_628_211
_UINT64_MASK = (1 << 64) - 1
_TOKEN = re.compile(r"[A-Za-z]+|[가-힣]+|\d+|[^\w\s]", re.UNICODE)
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

DENSE_FEATURE_NAMES = (
    "log_character_count",
    "log_word_count",
    "log_sentence_count",
    "log_message_count",
    "hangul_ratio",
    "log_code_marker_count",
    "log_math_marker_count",
    "numeric_density",
    "long_context",
    "log_reasoning_marker_count",
    "formal_reasoning",
    "program_analysis",
    "log_multi_constraint_count",
    "simple_transform",
)


@dataclass(frozen=True)
class LinearHead:
    intercept: float
    coefficients: Tuple[float, ...]


@dataclass(frozen=True)
class HashRegexArtifact:
    hash_bins: int
    feature_mean: Tuple[float, ...]
    feature_scale: Tuple[float, ...]
    score_heads: Mapping[str, LinearHead]
    log_cost_heads: Mapping[str, LinearHead]
    tier_safety_ratios: Mapping[str, float]
    policy_id: str
    policy_digest: str
    training_summary: Mapping[str, Any]


@dataclass(frozen=True)
class HashRegexPlan:
    submission: Submission
    predicted_budget_ratio: float
    safety_ratio: float
    ax31_fill_safety_ratio: Optional[float]


def _stable_hash(value: str) -> int:
    digest = _FNV_OFFSET
    for byte in value.encode("utf-8"):
        digest ^= byte
        digest = (digest * _FNV_PRIME) & _UINT64_MASK
    return digest


def _normalized_tokens(text: str) -> Tuple[str, ...]:
    result = []
    for token in _TOKEN.findall(text):
        normalized = token.casefold()
        if normalized.isdecimal():
            normalized = "<number>"
        result.append(normalized)
    return tuple(result)


def raw_feature_vector(episode: Episode, hash_bins: int) -> Tuple[float, ...]:
    """Build dense regex features plus signed word unigram/bigram bins."""

    if (
        isinstance(hash_bins, bool)
        or not isinstance(hash_bins, int)
        or not MIN_HASH_BINS <= hash_bins <= MAX_HASH_BINS
        or hash_bins & (hash_bins - 1)
    ):
        raise ValueError("hash_bins는 허용 범위의 2의 거듭제곱이어야 합니다.")
    features = extract_features(episode)
    text = episode_text(episode)
    dense = (
        math.log1p(features.character_count),
        math.log1p(features.word_count),
        math.log1p(features.sentence_count),
        math.log1p(features.message_count),
        features.hangul_ratio,
        math.log1p(features.code_marker_count),
        math.log1p(features.math_marker_count),
        features.numeric_density,
        float(features.long_context),
        math.log1p(features.reasoning_marker_count),
        float(bool(_FORMAL_REASONING.search(text))),
        float(bool(_PROGRAM_ANALYSIS.search(text))),
        math.log1p(len(_MULTI_CONSTRAINT.findall(text))),
        float(bool(_SIMPLE_TRANSFORM.search(text))),
    )
    bins = [0.0] * hash_bins
    tokens = _normalized_tokens(text)
    hashed_features = [f"w1:{token}" for token in tokens]
    hashed_features.extend(
        f"w2:{left}\x1f{right}" for left, right in zip(tokens, tokens[1:])
    )
    for value in hashed_features:
        digest = _stable_hash(value)
        index = digest & (hash_bins - 1)
        bins[index] += -1.0 if digest & (1 << 63) else 1.0
    norm = math.sqrt(sum(value * value for value in bins))
    if norm:
        bins = [value / norm for value in bins]
    return dense + tuple(bins)


def _object(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ProtocolError(f"{label}은(는) JSON 객체여야 합니다.")
    return value


def _exact_keys(value: Mapping[str, Any], expected: Sequence[str], label: str) -> None:
    missing = sorted(set(expected) - set(value))
    extra = sorted(set(value) - set(expected))
    if missing or extra:
        raise ProtocolError(f"{label} 필드 오류: 누락={missing}, 초과={extra}")


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise ProtocolError(f"{label} 값이 허용 범위를 벗어났습니다.")
    return value


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ProtocolError(f"{label}은(는) 유한한 숫자여야 합니다.")
    result = float(value)
    if not math.isfinite(result):
        raise ProtocolError(f"{label}은(는) 유한한 숫자여야 합니다.")
    return result


def _vector(value: Any, length: int, label: str) -> Tuple[float, ...]:
    if not isinstance(value, list) or len(value) != length:
        raise ProtocolError(f"{label}은(는) 길이 {length}의 배열이어야 합니다.")
    return tuple(_number(item, f"{label}[{index}]") for index, item in enumerate(value))


def _head(value: Any, length: int, label: str) -> LinearHead:
    raw = _object(value, label)
    _exact_keys(raw, ("intercept", "coefficients"), label)
    return LinearHead(
        intercept=_number(raw["intercept"], f"{label}.intercept"),
        coefficients=_vector(
            raw["coefficients"], length, f"{label}.coefficients"
        ),
    )


def parse_artifact(value: Any) -> HashRegexArtifact:
    root = _object(value, "artifact")
    expected = (
        "artifact_type",
        "schema_version",
        "feature_version",
        "hash_algorithm",
        "hash_bins",
        "dense_feature_names",
        "model_ids",
        "policy_id",
        "policy_sha256",
        "feature_mean",
        "feature_scale",
        "score_heads",
        "log_cost_heads",
        "tier_safety_ratios",
        "training_summary",
    )
    _exact_keys(root, expected, "artifact")
    if root["artifact_type"] != ARTIFACT_TYPE:
        raise ProtocolError("지원하지 않는 hash-regex artifact_type입니다.")
    if (
        _integer(root["schema_version"], "artifact.schema_version", 1, 1) != 1
        or _integer(
            root["feature_version"],
            "artifact.feature_version",
            FEATURE_VERSION,
            FEATURE_VERSION,
        )
        != FEATURE_VERSION
    ):
        raise ProtocolError("지원하지 않는 hash-regex artifact 버전입니다.")
    if root["hash_algorithm"] != "fnv1a64-signed-word-1-2":
        raise ProtocolError("지원하지 않는 feature hash 방식입니다.")
    hash_bins = _integer(
        root["hash_bins"], "artifact.hash_bins", MIN_HASH_BINS, MAX_HASH_BINS
    )
    if hash_bins & (hash_bins - 1):
        raise ProtocolError("artifact.hash_bins는 2의 거듭제곱이어야 합니다.")
    if root["dense_feature_names"] != list(DENSE_FEATURE_NAMES):
        raise ProtocolError("dense feature 정의가 현재 런타임과 다릅니다.")
    if root["model_ids"] != list(MODEL_IDS):
        raise ProtocolError("artifact.model_ids가 공개 정책 모델과 다릅니다.")
    length = len(DENSE_FEATURE_NAMES) + hash_bins
    mean = _vector(root["feature_mean"], length, "artifact.feature_mean")
    scale = _vector(root["feature_scale"], length, "artifact.feature_scale")
    if any(item <= 0 for item in scale):
        raise ProtocolError("artifact.feature_scale은 모두 0보다 커야 합니다.")
    score_raw = _object(root["score_heads"], "artifact.score_heads")
    cost_raw = _object(root["log_cost_heads"], "artifact.log_cost_heads")
    if set(score_raw) != set(MODEL_IDS) or set(cost_raw) != set(MODEL_IDS):
        raise ProtocolError("artifact 선형 head의 모델 집합이 올바르지 않습니다.")
    safety_raw = _object(
        root["tier_safety_ratios"], "artifact.tier_safety_ratios"
    )
    if set(safety_raw) != set(TIERS):
        raise ProtocolError("artifact 등급별 안전계수가 완전하지 않습니다.")
    safety = {
        tier: _number(safety_raw[tier], f"artifact.tier_safety_ratios.{tier}")
        for tier in TIERS
    }
    if any(not 0 < value <= 1 for value in safety.values()):
        raise ProtocolError("artifact 안전계수는 0보다 크고 1 이하여야 합니다.")
    policy_id = root["policy_id"]
    policy_digest = root["policy_sha256"]
    if not isinstance(policy_id, str) or not policy_id:
        raise ProtocolError("artifact.policy_id가 올바르지 않습니다.")
    if (
        not isinstance(policy_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", policy_digest) is None
    ):
        raise ProtocolError("artifact.policy_sha256가 올바르지 않습니다.")
    training_summary = _object(root["training_summary"], "artifact.training_summary")
    return HashRegexArtifact(
        hash_bins=hash_bins,
        feature_mean=mean,
        feature_scale=scale,
        score_heads={
            model_id: _head(score_raw[model_id], length, f"score_heads.{model_id}")
            for model_id in MODEL_IDS
        },
        log_cost_heads={
            model_id: _head(cost_raw[model_id], length, f"log_cost_heads.{model_id}")
            for model_id in MODEL_IDS
        },
        tier_safety_ratios=safety,
        policy_id=policy_id,
        policy_digest=policy_digest,
        training_summary=dict(training_summary),
    )


def load_artifact(path: Path) -> HashRegexArtifact:
    return parse_artifact(load_json(path))


def _linear(head: LinearHead, values: Sequence[float]) -> float:
    return head.intercept + math.fsum(
        coefficient * value
        for coefficient, value in zip(head.coefficients, values)
    )


def predict_episode(
    episode: Episode, artifact: HashRegexArtifact
) -> Tuple[Mapping[str, float], Mapping[str, float]]:
    raw = raw_feature_vector(episode, artifact.hash_bins)
    standardized = tuple(
        (value - mean) / scale
        for value, mean, scale in zip(
            raw, artifact.feature_mean, artifact.feature_scale
        )
    )
    scores = {
        model_id: min(1.0, max(0.0, _linear(artifact.score_heads[model_id], standardized)))
        for model_id in MODEL_IDS
    }
    costs = {
        model_id: math.exp(
            min(50.0, max(-50.0, _linear(artifact.log_cost_heads[model_id], standardized)))
        )
        for model_id in MODEL_IDS
    }
    light = costs[MODEL_IDS[0]]
    costs[MODEL_IDS[1]] = max(costs[MODEL_IDS[1]], light * (1.0 + 1e-12))
    costs[MODEL_IDS[2]] = max(
        costs[MODEL_IDS[2]], costs[MODEL_IDS[1]] * (1.0 + 1e-12)
    )
    return scores, costs


def select_models(
    predicted_scores: Sequence[Mapping[str, float]],
    predicted_costs: Sequence[Mapping[str, float]],
    *,
    budget_multiplier: float,
    safety_ratio: float,
) -> Tuple[Tuple[str, ...], float]:
    """Select one model per row with a batch-level Lagrangian budget."""

    if len(predicted_scores) != len(predicted_costs) or not predicted_scores:
        raise ValueError("예측 score와 cost는 같은 길이의 비어 있지 않은 배열이어야 합니다.")
    light_total = math.fsum(row[MODEL_IDS[0]] for row in predicted_costs)
    effective_ratio = max(1.0, budget_multiplier * safety_ratio)
    cap = light_total * effective_ratio

    def choose(penalty: float) -> Tuple[Tuple[str, ...], float]:
        selected = []
        for scores, costs in zip(predicted_scores, predicted_costs):
            model_id = max(
                MODEL_IDS,
                key=lambda candidate: (
                    scores[candidate]
                    - penalty * costs[candidate] / light_total,
                    -MODEL_IDS.index(candidate),
                ),
            )
            selected.append(model_id)
        total = math.fsum(
            costs[model_id]
            for costs, model_id in zip(predicted_costs, selected)
        )
        return tuple(selected), total

    selected, total = choose(0.0)
    if total > cap:
        low = 0.0
        high = 1.0
        selected, total = choose(high)
        while total > cap and high < 2**60:
            low = high
            high *= 2.0
            selected, total = choose(high)
        for _iteration in range(80):
            middle = (low + high) / 2.0
            candidate, candidate_total = choose(middle)
            if candidate_total <= cap:
                high = middle
                selected, total = candidate, candidate_total
            else:
                low = middle
    if total > cap:
        selected = tuple(MODEL_IDS[0] for _row in predicted_scores)
        total = light_total
    return selected, total / light_total


def fill_ax31_upgrades(
    selected: Sequence[str],
    predicted_scores: Sequence[Mapping[str, float]],
    predicted_costs: Sequence[Mapping[str, float]],
    *,
    budget_multiplier: float,
    safety_ratio: float,
) -> Tuple[Tuple[str, ...], float]:
    """Lock existing choices and fill unused predicted budget with AX31 upgrades."""

    if (
        len(selected) != len(predicted_scores)
        or len(selected) != len(predicted_costs)
        or not selected
    ):
        raise ValueError("선택과 예측 배열은 같은 길이의 비어 있지 않은 배열이어야 합니다.")
    if any(model_id not in MODEL_IDS for model_id in selected):
        raise ValueError("선택 배열에 알 수 없는 모델이 있습니다.")
    if not 0 < safety_ratio <= 1:
        raise ValueError("AX31 fill 안전계수는 0보다 크고 1 이하여야 합니다.")

    light_id, ax31_id, _premium_id = MODEL_IDS
    light_total = math.fsum(row[light_id] for row in predicted_costs)
    current_total = math.fsum(
        costs[model_id]
        for costs, model_id in zip(predicted_costs, selected)
    )
    cap = max(
        current_total,
        light_total * max(1.0, budget_multiplier * safety_ratio),
    )

    def choose(penalty: float) -> Tuple[Tuple[str, ...], float]:
        filled = []
        for model_id, scores, costs in zip(
            selected, predicted_scores, predicted_costs
        ):
            if model_id != light_id:
                filled.append(model_id)
                continue
            incremental_score = scores[ax31_id] - scores[light_id]
            incremental_cost = costs[ax31_id] - costs[light_id]
            if incremental_score - penalty * incremental_cost / light_total > 0:
                filled.append(ax31_id)
            else:
                filled.append(light_id)
        total = math.fsum(
            costs[model_id]
            for costs, model_id in zip(predicted_costs, filled)
        )
        return tuple(filled), total

    filled, total = choose(0.0)
    if total > cap:
        low = 0.0
        high = 1.0
        filled, total = choose(high)
        while total > cap and high < 2**60:
            low = high
            high *= 2.0
            filled, total = choose(high)
        for _iteration in range(80):
            middle = (low + high) / 2.0
            candidate, candidate_total = choose(middle)
            if candidate_total <= cap:
                high = middle
                filled, total = candidate, candidate_total
            else:
                low = middle
    if total > cap:
        return tuple(selected), current_total / light_total
    return filled, total / light_total


def make_hash_regex_submission(
    inputs: InputBatch,
    policy: RoutingPolicy,
    artifact: HashRegexArtifact,
    tier: str,
) -> HashRegexPlan:
    if inputs.schema_version != policy.schema_version:
        raise ProtocolError("입력과 정책의 schema_version이 일치하지 않습니다.")
    if tier not in TIERS:
        raise ProtocolError(f"알 수 없는 tier: {tier}")
    if artifact.policy_id != policy.policy_id:
        raise ProtocolError("artifact와 정책의 policy_id가 다릅니다.")
    if artifact.policy_digest != policy_sha256(policy):
        raise ProtocolError("artifact와 현재 정책의 SHA-256이 다릅니다.")
    predictions = [predict_episode(episode, artifact) for episode in inputs.episodes]
    scores = [item[0] for item in predictions]
    costs = [item[1] for item in predictions]
    safety = artifact.tier_safety_ratios[tier]
    selected, ratio = select_models(
        scores,
        costs,
        budget_multiplier=float(policy.tiers[tier].budget_multiplier),
        safety_ratio=safety,
    )
    fill_safety = None
    if tier == "premium":
        fill_safety = PREMIUM_AX31_FILL_SAFETY_RATIO
        selected, ratio = fill_ax31_upgrades(
            selected,
            scores,
            costs,
            budget_multiplier=float(policy.tiers[tier].budget_multiplier),
            safety_ratio=fill_safety,
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
    return HashRegexPlan(
        submission=parse_submission(submission_to_dict(submission)),
        predicted_budget_ratio=ratio,
        safety_ratio=safety,
        ax31_fill_safety_ratio=fill_safety,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="학습된 regex·feature-hashing baseline 라우터"
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--tier", choices=TIERS, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
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
        artifact = load_artifact(args.artifact)
        plan = make_hash_regex_submission(inputs, policy, artifact, args.tier)
        write_submission_atomic(args.output, plan.submission)
    except (OSError, ProtocolError, ValueError, json.JSONDecodeError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2
    safety_message = f"기본 안전계수 {plan.safety_ratio:.4f}"
    if plan.ax31_fill_safety_ratio is not None:
        safety_message += f", AX31 fill 안전계수 {plan.ax31_fill_safety_ratio:.4f}"
    print(
        "OK: "
        f"{args.tier} 제출 파일을 생성했습니다 "
        f"(예측 비용 비율 {plan.predicted_budget_ratio:.6f}, {safety_message})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
