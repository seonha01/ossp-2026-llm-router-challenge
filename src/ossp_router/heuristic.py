# SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
# SPDX-License-Identifier: Apache-2.0

"""Deliberately weak, prompt-only reference router."""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from .protocol import (
    TIERS,
    Decision,
    Episode,
    InputBatch,
    ProtocolError,
    RoutingPolicy,
    Submission,
    dumps_json,
    load_bundled_policy,
    load_input,
    load_policy,
    parse_submission,
    submission_to_dict,
)


_CODE_MARKERS = re.compile(
    r"```|(?:^|\s)(?:def|class|function|SELECT|FROM|import|#include)\b|"
    r"[{};]\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_MATH_MARKERS = re.compile(r"[=+\-*/^∑∫√≈≠≤≥<>]|\\(?:frac|sum|int|sqrt)\b")
_NUMBER = re.compile(r"\d")
_WORD = re.compile(r"[A-Za-z가-힣]+")
_SENTENCE_END = re.compile(r"[.!?。！？]")
_REASONING_WORDS = re.compile(
    r"\b(?:prove|derive|reason|analyze|explain why|algorithm|complexity|"
    r"증명|유도|추론|분석|알고리즘|복잡도)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PromptFeatures:
    """Small set of directly computed, content-only features."""

    character_count: int
    message_count: int
    word_count: int
    sentence_count: int
    hangul_ratio: float
    code_marker_count: int
    math_marker_count: int
    numeric_density: float
    long_context: bool
    reasoning_marker_count: int


def episode_text(episode: Episode) -> str:
    """Return only the prompt or message content available at routing time."""

    if episode.prompt is not None:
        return episode.prompt
    assert episode.messages is not None
    return "\n".join(message.content for message in episode.messages)


def extract_features(episode: Episode) -> PromptFeatures:
    """Compute simple features without reading an ID, position, or metadata."""

    text = episode_text(episode)
    characters = len(text)
    nonspace = sum(not character.isspace() for character in text)
    hangul = sum("\uac00" <= character <= "\ud7a3" for character in text)
    numbers = len(_NUMBER.findall(text))
    message_count = 1 if episode.prompt is not None else len(episode.messages or ())
    return PromptFeatures(
        character_count=characters,
        message_count=message_count,
        word_count=len(_WORD.findall(text)),
        sentence_count=max(1, len(_SENTENCE_END.findall(text))),
        hangul_ratio=hangul / max(1, nonspace),
        code_marker_count=len(_CODE_MARKERS.findall(text)),
        math_marker_count=len(_MATH_MARKERS.findall(text)),
        numeric_density=numbers / max(1, nonspace),
        long_context=characters >= 8_000,
        reasoning_marker_count=len(_REASONING_WORDS.findall(text)),
    )


def complexity_score(features: PromptFeatures) -> int:
    """Return a deliberately coarse score used only by the reference baseline."""

    score = 0
    if features.character_count >= 500:
        score += 1
    if features.character_count >= 2_000:
        score += 1
    if features.long_context:
        score += 2
    if features.message_count >= 3:
        score += 1
    if features.code_marker_count:
        score += 2
    if features.math_marker_count >= 2 or features.numeric_density >= 0.08:
        score += 2
    elif features.math_marker_count:
        score += 1
    if features.reasoning_marker_count:
        score += 1
    if features.word_count >= 350 or features.sentence_count >= 20:
        score += 1
    return score


def select_model(features: PromptFeatures, tier: str) -> str:
    """Choose a model using fixed, intentionally conservative thresholds."""

    if tier not in TIERS:
        raise ProtocolError(f"알 수 없는 tier: {tier}")
    score = complexity_score(features)
    if tier == "fast":
        return (
            "ax31" if score >= 3 and not features.long_context else "ax31-light"
        )
    if tier == "balanced":
        return (
            "ax31" if score >= 2 and not features.long_context else "ax31-light"
        )
    # Without a learned output-length estimate, this deliberately weak baseline
    # avoids the much less predictable think-model cost.
    return "ax31"


def make_submission(
    inputs: InputBatch,
    policy: RoutingPolicy,
    tier: str,
    *,
    strategy: str = "prompt-heuristic",
) -> Submission:
    """Create one complete v1 submission for a single tier."""

    if inputs.schema_version != policy.schema_version:
        raise ProtocolError("입력과 정책의 schema_version이 일치하지 않습니다.")
    if tier not in TIERS:
        raise ProtocolError(f"알 수 없는 tier: {tier}")
    if strategy not in {"always-light", "prompt-heuristic"}:
        raise ValueError(f"알 수 없는 baseline 라우터: {strategy}")
    decisions = []
    for episode in inputs.episodes:
        model_id = (
            policy.light_model_id
            if strategy == "always-light"
            else select_model(extract_features(episode), tier)
        )
        decisions.append(Decision(episode.episode_id, model_id))
    submission = Submission(
        schema_version=inputs.schema_version,
        challenge_id=inputs.challenge_id,
        policy_id=policy.policy_id,
        split=inputs.split,
        tier=tier,
        decisions=tuple(decisions),
    )
    # Keep the generator and the public v1 parser on the same strict path.
    return parse_submission(submission_to_dict(submission))


def write_submission_atomic(path: Path, submission: Submission) -> None:
    """Write one result without leaving a valid-looking partial JSON file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        temporary.write_text(
            dumps_json(submission_to_dict(submission)),
            encoding="utf-8",
        )
        temporary.chmod(0o644)
        os.replace(str(temporary), str(path))
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="router-run",
        description="프롬프트 기반 baseline 라우터를 한 등급에 대해 실행합니다.",
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
        submission = make_submission(inputs, policy, args.tier)
        write_submission_atomic(args.output, submission)
    except (OSError, ProtocolError, ValueError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2
    print(f"OK: {args.tier} 제출 파일을 생성했습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
