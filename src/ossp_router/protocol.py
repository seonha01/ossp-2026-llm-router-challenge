# SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
# SPDX-License-Identifier: Apache-2.0

"""Frozen v1 wire protocol for prompt-only router evaluation."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from importlib import resources
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


SCHEMA_VERSION = 1
POLICY_ID = "ossp-2026-prompt-router-v1"
MODEL_IDS = ("ax31-light", "ax31", "axk1-think")
TIERS = ("fast", "balanced", "premium")
MESSAGE_ROLES = ("system", "user", "assistant")
SCORE_DECIMAL_PLACES = 12
SCORE_ROUNDING = "ROUND_HALF_EVEN"
DECIMAL_MAX_DIGITS = 40
DECIMAL_MAX_FRACTIONAL_DIGITS = 30
_DECIMAL_STRING = re.compile(
    rf"^(0|[1-9][0-9]{{0,{DECIMAL_MAX_DIGITS - 1}}})"
    rf"(\.[0-9]{{1,{DECIMAL_MAX_FRACTIONAL_DIGITS}}})?$"
)


class ProtocolError(ValueError):
    """Raised when an input does not satisfy the public protocol."""


@dataclass(frozen=True)
class Message:
    role: str
    content: str


@dataclass(frozen=True)
class Episode:
    episode_id: str
    prompt: Optional[str] = None
    messages: Optional[Tuple[Message, ...]] = None


@dataclass(frozen=True)
class InputBatch:
    schema_version: int
    challenge_id: str
    split: str
    episodes: Tuple[Episode, ...]


@dataclass(frozen=True)
class Outcome:
    episode_id: str
    model_id: str
    score: Decimal
    num_generations: int
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class OutcomeBatch:
    schema_version: int
    challenge_id: str
    split: str
    outcomes: Tuple[Outcome, ...]


@dataclass(frozen=True)
class Decision:
    episode_id: str
    model_id: str


@dataclass(frozen=True)
class Submission:
    schema_version: int
    challenge_id: str
    policy_id: str
    split: str
    tier: str
    decisions: Tuple[Decision, ...]


@dataclass(frozen=True)
class ModelRates:
    fixed_cost: Decimal
    input_token_rate: Decimal
    output_token_rate: Decimal


@dataclass(frozen=True)
class TierPolicy:
    budget_multiplier: Decimal
    weight: Decimal


@dataclass(frozen=True)
class RoutingPolicy:
    schema_version: int
    policy_id: str
    cost_unit: str
    token_unit: int
    context_limit_tokens: int
    light_model_id: str
    models: Mapping[str, ModelRates]
    tiers: Mapping[str, TierPolicy]
    budget_warning_ratio: Decimal


def _reject_constant(value: str) -> None:
    raise ProtocolError(f"JSON 상수 {value!r}는 허용되지 않습니다.")


def _unique_object(pairs: Sequence[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError(f"중복 JSON 키: {key}")
        result[key] = value
    return result


def loads_json(text: str) -> Any:
    """Load JSON while preserving decimals and rejecting duplicate keys."""

    try:
        return json.loads(
            text,
            parse_float=Decimal,
            parse_int=int,
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_object,
        )
    except (json.JSONDecodeError, InvalidOperation) as exc:
        raise ProtocolError(f"올바르지 않은 JSON: {exc}") from exc


def load_json(path: Path) -> Any:
    try:
        return loads_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        raise ProtocolError(f"{path} 파일을 읽을 수 없습니다: {exc}") from exc


def _object(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ProtocolError(f"{label}은(는) JSON 객체여야 합니다.")
    return value


def _array(value: Any, label: str, *, nonempty: bool = False) -> Sequence[Any]:
    if not isinstance(value, list):
        raise ProtocolError(f"{label}은(는) JSON 배열이어야 합니다.")
    if nonempty and not value:
        raise ProtocolError(f"{label}은(는) 비어 있을 수 없습니다.")
    return value


def _exact_keys(
    value: Mapping[str, Any],
    *,
    required: Iterable[str],
    optional: Iterable[str] = (),
    label: str,
) -> None:
    required_set = set(required)
    allowed = required_set | set(optional)
    missing = sorted(required_set - set(value))
    extra = sorted(set(value) - allowed)
    if missing or extra:
        details = []
        if missing:
            details.append(f"누락={missing}")
        if extra:
            details.append(f"허용되지 않은 필드={extra}")
        raise ProtocolError(f"{label} 필드 오류: {', '.join(details)}")


def _text(
    value: Any,
    label: str,
    *,
    max_length: Optional[int] = None,
    nonblank: bool = False,
) -> str:
    if not isinstance(value, str) or not value:
        raise ProtocolError(f"{label}은(는) 비어 있지 않은 문자열이어야 합니다.")
    if max_length is not None and len(value) > max_length:
        raise ProtocolError(f"{label}은(는) {max_length}자를 넘을 수 없습니다.")
    if nonblank and not value.strip():
        raise ProtocolError(f"{label}은(는) 공백으로만 구성할 수 없습니다.")
    return value


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ProtocolError(f"{label}은(는) {minimum} 이상의 정수여야 합니다.")
    return value


def _decimal(
    value: Any,
    label: str,
    *,
    minimum: Optional[Decimal] = None,
    maximum: Optional[Decimal] = None,
    allow_zero: bool = True,
) -> Decimal:
    if (
        not isinstance(value, str)
        or _DECIMAL_STRING.fullmatch(value) is None
        or len(value.replace(".", "")) > DECIMAL_MAX_DIGITS
    ):
        raise ProtocolError(
            f"{label}은(는) 지수 표기 없는 음이 아닌 십진 문자열이어야 합니다."
        )
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise ProtocolError(f"{label}은(는) 유한한 십진수여야 합니다.") from exc
    if not allow_zero and result == 0:
        raise ProtocolError(f"{label}은(는) 0보다 커야 합니다.")
    if minimum is not None and result < minimum:
        raise ProtocolError(f"{label}은(는) {minimum} 이상이어야 합니다.")
    if maximum is not None and result > maximum:
        raise ProtocolError(f"{label}은(는) {maximum} 이하여야 합니다.")
    return result


def _schema_version(value: Any, label: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value != SCHEMA_VERSION
    ):
        raise ProtocolError(f"지원하지 않는 {label}: {value!r}")
    return SCHEMA_VERSION


def _common_header(value: Mapping[str, Any], label: str) -> Tuple[int, str, str]:
    schema_version = _schema_version(
        value.get("schema_version"), f"{label}.schema_version"
    )
    challenge_id = _text(value.get("challenge_id"), f"{label}.challenge_id")
    split = _text(value.get("split"), f"{label}.split")
    return schema_version, challenge_id, split


def parse_input(value: Any) -> InputBatch:
    root = _object(value, "입력")
    _exact_keys(
        root,
        required=("schema_version", "challenge_id", "split", "episodes"),
        label="입력",
    )
    schema_version, challenge_id, split = _common_header(root, "입력")
    episodes: List[Episode] = []
    seen = set()
    for index, item in enumerate(_array(root["episodes"], "episodes", nonempty=True)):
        episode = _object(item, f"episodes[{index}]")
        _exact_keys(
            episode,
            required=("episode_id",),
            optional=("prompt", "messages"),
            label=f"episodes[{index}]",
        )
        episode_id = _text(
            episode["episode_id"],
            f"episodes[{index}].episode_id",
            max_length=128,
        )
        if episode_id in seen:
            raise ProtocolError(f"중복 episode_id: {episode_id}")
        seen.add(episode_id)
        has_prompt = "prompt" in episode
        has_messages = "messages" in episode
        if has_prompt == has_messages:
            raise ProtocolError(
                f"episode {episode_id}에는 prompt 또는 messages 중 하나만 있어야 합니다."
            )
        if has_prompt:
            episodes.append(
                Episode(
                    episode_id=episode_id,
                    prompt=_text(episode["prompt"], f"episode {episode_id}.prompt"),
                )
            )
            continue
        messages: List[Message] = []
        for message_index, raw_message in enumerate(
            _array(
                episode["messages"],
                f"episode {episode_id}.messages",
                nonempty=True,
            )
        ):
            message = _object(raw_message, f"message[{message_index}]")
            _exact_keys(
                message,
                required=("role", "content"),
                label=f"message[{message_index}]",
            )
            role = _text(message["role"], f"message[{message_index}].role")
            if role not in MESSAGE_ROLES:
                raise ProtocolError(f"알 수 없는 message role: {role}")
            messages.append(
                Message(
                    role=role,
                    content=_text(
                        message["content"], f"message[{message_index}].content"
                    ),
                )
            )
        episodes.append(Episode(episode_id=episode_id, messages=tuple(messages)))
    return InputBatch(schema_version, challenge_id, split, tuple(episodes))


def parse_outcomes(value: Any) -> OutcomeBatch:
    root = _object(value, "outcomes")
    _exact_keys(
        root,
        required=("schema_version", "challenge_id", "split", "episodes"),
        label="outcomes",
    )
    schema_version, challenge_id, split = _common_header(root, "outcomes")
    outcomes: List[Outcome] = []
    seen_episodes = set()
    outcome_fields = (
        "score",
        "num_generations",
        "input_tokens",
        "output_tokens",
    )
    for episode_index, item in enumerate(
        _array(root["episodes"], "outcomes.episodes", nonempty=True)
    ):
        episode = _object(item, f"outcomes.episodes[{episode_index}]")
        _exact_keys(
            episode,
            required=("episode_id", "models"),
            label=f"outcomes.episodes[{episode_index}]",
        )
        episode_id = _text(
            episode["episode_id"],
            f"outcomes.episodes[{episode_index}].episode_id",
            max_length=128,
        )
        if episode_id in seen_episodes:
            raise ProtocolError(f"중복 outcome episode_id: {episode_id}")
        seen_episodes.add(episode_id)
        models = _object(episode["models"], f"outcome {episode_id}.models")
        if set(models) != set(MODEL_IDS):
            raise ProtocolError(
                f"outcome {episode_id}.models는 정확히 {list(MODEL_IDS)}여야 합니다."
            )
        for model_id in MODEL_IDS:
            raw = _object(models[model_id], f"outcome {episode_id}/{model_id}")
            _exact_keys(
                raw,
                required=outcome_fields,
                label=f"outcome {episode_id}/{model_id}",
            )
            num_generations = _integer(
                raw["num_generations"],
                f"outcome {episode_id}/{model_id}.num_generations",
                minimum=1,
            )
            outcomes.append(
                Outcome(
                    episode_id=episode_id,
                    model_id=model_id,
                    score=_decimal(
                        raw["score"],
                        f"outcome {episode_id}/{model_id}.score",
                        minimum=Decimal("0"),
                        maximum=Decimal("1"),
                    ),
                    num_generations=num_generations,
                    input_tokens=_integer(
                        raw["input_tokens"],
                        f"outcome {episode_id}/{model_id}.input_tokens",
                        minimum=1,
                    ),
                    output_tokens=_integer(
                        raw["output_tokens"],
                        f"outcome {episode_id}/{model_id}.output_tokens",
                    ),
                )
            )
    return OutcomeBatch(schema_version, challenge_id, split, tuple(outcomes))


def parse_submission(value: Any) -> Submission:
    root = _object(value, "submission")
    _exact_keys(
        root,
        required=(
            "schema_version",
            "challenge_id",
            "policy_id",
            "split",
            "tier",
            "decisions",
        ),
        label="submission",
    )
    schema_version, challenge_id, split = _common_header(root, "submission")
    policy_id = _text(root["policy_id"], "submission.policy_id")
    tier = _text(root["tier"], "submission.tier")
    if tier not in TIERS:
        raise ProtocolError(f"알 수 없는 tier: {tier}")
    decisions: List[Decision] = []
    seen = set()
    for index, item in enumerate(
        _array(root["decisions"], "submission.decisions", nonempty=True)
    ):
        raw = _object(item, f"decisions[{index}]")
        _exact_keys(
            raw,
            required=("episode_id", "model_id"),
            label=f"decisions[{index}]",
        )
        episode_id = _text(
            raw["episode_id"], f"decisions[{index}].episode_id", max_length=128
        )
        if episode_id in seen:
            raise ProtocolError(f"중복 decision episode_id: {episode_id}")
        seen.add(episode_id)
        model_id = _text(raw["model_id"], f"decisions[{index}].model_id")
        if model_id not in MODEL_IDS:
            raise ProtocolError(f"알 수 없는 model_id: {model_id}")
        decisions.append(Decision(episode_id, model_id))
    return Submission(
        schema_version, challenge_id, policy_id, split, tier, tuple(decisions)
    )


def parse_policy(value: Any) -> RoutingPolicy:
    root = _object(value, "policy")
    _exact_keys(
        root,
        required=(
            "schema_version",
            "policy_id",
            "cost_unit",
            "context_limit_tokens",
            "light_model_id",
            "models",
            "tiers",
        ),
        optional=("token_unit", "budget_warning_ratio"),
        label="policy",
    )
    schema_version = _schema_version(
        root["schema_version"], "policy.schema_version"
    )
    policy_id = _text(root["policy_id"], "policy.policy_id")
    if policy_id != POLICY_ID:
        raise ProtocolError(f"지원하지 않는 policy_id: {policy_id}")
    cost_unit = _text(root["cost_unit"], "policy.cost_unit")
    token_unit = _integer(
        root.get("token_unit", 1_000_000), "policy.token_unit", minimum=1
    )
    if token_unit != 1_000_000:
        raise ProtocolError("policy.token_unit은 v1에서 1000000이어야 합니다.")
    context_limit_tokens = _integer(
        root["context_limit_tokens"],
        "policy.context_limit_tokens",
        minimum=1,
    )
    light_model_id = _text(root["light_model_id"], "policy.light_model_id")

    raw_models = _object(root["models"], "policy.models")
    if set(raw_models) != set(MODEL_IDS):
        raise ProtocolError(f"policy.models는 정확히 {list(MODEL_IDS)}여야 합니다.")
    models: Dict[str, ModelRates] = {}
    for model_id in MODEL_IDS:
        raw_model = _object(raw_models[model_id], f"policy.models.{model_id}")
        _exact_keys(
            raw_model,
            required=("input_token_rate", "output_token_rate"),
            optional=("fixed_cost",),
            label=f"policy.models.{model_id}",
        )
        models[model_id] = ModelRates(
            fixed_cost=_decimal(
                raw_model.get("fixed_cost", "0"),
                f"{model_id}.fixed_cost",
                minimum=Decimal("0"),
            ),
            input_token_rate=_decimal(
                raw_model["input_token_rate"],
                f"{model_id}.input_token_rate",
                minimum=Decimal("0"),
            ),
            output_token_rate=_decimal(
                raw_model["output_token_rate"],
                f"{model_id}.output_token_rate",
                minimum=Decimal("0"),
            ),
        )
    if light_model_id not in models:
        raise ProtocolError("policy.light_model_id는 후보 모델 중 하나여야 합니다.")

    raw_tiers = _object(root["tiers"], "policy.tiers")
    if set(raw_tiers) != set(TIERS):
        raise ProtocolError(f"policy.tiers는 정확히 {list(TIERS)}여야 합니다.")
    tiers: Dict[str, TierPolicy] = {}
    for tier in TIERS:
        raw_tier = _object(raw_tiers[tier], f"policy.tiers.{tier}")
        _exact_keys(
            raw_tier,
            required=("budget_multiplier", "weight"),
            label=f"policy.tiers.{tier}",
        )
        tiers[tier] = TierPolicy(
            budget_multiplier=_decimal(
                raw_tier["budget_multiplier"],
                f"{tier}.budget_multiplier",
                minimum=Decimal("0"),
                allow_zero=False,
            ),
            weight=_decimal(
                raw_tier["weight"],
                f"{tier}.weight",
                minimum=Decimal("0"),
            ),
        )
    if sum((tier.weight for tier in tiers.values()), Decimal("0")) != Decimal("1"):
        raise ProtocolError("tier weight의 합은 정확히 1이어야 합니다.")
    budget_warning_ratio = _decimal(
        root.get("budget_warning_ratio", "0.95"),
        "policy.budget_warning_ratio",
        minimum=Decimal("0"),
        maximum=Decimal("1"),
        allow_zero=False,
    )
    return RoutingPolicy(
        schema_version=schema_version,
        policy_id=policy_id,
        cost_unit=cost_unit,
        token_unit=token_unit,
        context_limit_tokens=context_limit_tokens,
        light_model_id=light_model_id,
        models=models,
        tiers=tiers,
        budget_warning_ratio=budget_warning_ratio,
    )


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def policy_to_dict(policy: RoutingPolicy) -> Dict[str, Any]:
    return {
        "schema_version": policy.schema_version,
        "policy_id": policy.policy_id,
        "cost_unit": policy.cost_unit,
        "token_unit": policy.token_unit,
        "context_limit_tokens": policy.context_limit_tokens,
        "light_model_id": policy.light_model_id,
        "models": {
            model_id: {
                "fixed_cost": _decimal_text(rates.fixed_cost),
                "input_token_rate": _decimal_text(rates.input_token_rate),
                "output_token_rate": _decimal_text(rates.output_token_rate),
            }
            for model_id, rates in policy.models.items()
        },
        "tiers": {
            tier: {
                "budget_multiplier": _decimal_text(item.budget_multiplier),
                "weight": _decimal_text(item.weight),
            }
            for tier, item in policy.tiers.items()
        },
        "budget_warning_ratio": _decimal_text(policy.budget_warning_ratio),
    }


def policy_sha256(policy: RoutingPolicy) -> str:
    canonical = json.dumps(
        policy_to_dict(policy),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_input(path: Path) -> InputBatch:
    return parse_input(load_json(path))


def load_outcomes(path: Path) -> OutcomeBatch:
    return parse_outcomes(load_json(path))


def load_submission(path: Path) -> Submission:
    return parse_submission(load_json(path))


def load_policy(path: Path) -> RoutingPolicy:
    return parse_policy(load_json(path))


def load_bundled_policy() -> RoutingPolicy:
    try:
        text = resources.read_text(
            "ossp_router.resources",
            "routing-policy.v1.json",
            encoding="utf-8",
        )
    except (OSError, UnicodeError) as exc:
        raise ProtocolError(f"내장 정책 파일을 읽을 수 없습니다: {exc}") from exc
    return parse_policy(loads_json(text))


def submission_to_dict(submission: Submission) -> Dict[str, Any]:
    return {
        "schema_version": submission.schema_version,
        "challenge_id": submission.challenge_id,
        "policy_id": submission.policy_id,
        "split": submission.split,
        "tier": submission.tier,
        "decisions": [asdict(decision) for decision in submission.decisions],
    }


def dumps_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dumps_json(value), encoding="utf-8")
