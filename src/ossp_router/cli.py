# SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
# SPDX-License-Identifier: Apache-2.0

"""Command-line interface for validation, baseline generation, and scoring."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

from .protocol import (
    TIERS,
    Decision,
    ProtocolError,
    Submission,
    load_input,
    load_bundled_policy,
    load_outcomes,
    load_policy,
    load_submission,
    submission_to_dict,
    write_json,
)
from .scoring import ScoringError, score_submissions


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ossp-router",
        description="OSSP 2026 LLM Router Challenge 로컬 검증 도구",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_input = subparsers.add_parser(
        "validate-input", help="공개 입력 JSON의 프로토콜을 검증합니다."
    )
    validate_input.add_argument("--input", type=Path, required=True)

    always_light = subparsers.add_parser(
        "always-light", help="모든 episode에 ax31-light를 선택하는 제출을 만듭니다."
    )
    always_light.add_argument("--input", type=Path, required=True)
    always_light.add_argument("--output-dir", type=Path, required=True)
    always_light.add_argument("--policy", type=Path)

    self_check = subparsers.add_parser(
        "self-check", help="세 tier 제출을 검증하고 로컬 점수를 계산합니다."
    )
    self_check.add_argument("--input", type=Path, required=True)
    self_check.add_argument("--outcomes", type=Path, required=True)
    self_check.add_argument("--submissions", type=Path, required=True)
    self_check.add_argument("--policy", type=Path)
    self_check.add_argument("--report", type=Path, required=True)
    return parser


def _load_cli_policy(policy_path: Optional[Path]):
    return load_policy(policy_path) if policy_path is not None else load_bundled_policy()


def _always_light(
    input_path: Path, output_dir: Path, policy_path: Optional[Path]
) -> None:
    inputs = load_input(input_path)
    policy = _load_cli_policy(policy_path)
    if inputs.schema_version != policy.schema_version:
        raise ProtocolError("입력과 정책의 schema_version이 일치하지 않습니다.")
    decisions = tuple(
        Decision(episode.episode_id, policy.light_model_id)
        for episode in inputs.episodes
    )
    for tier in TIERS:
        submission = Submission(
            schema_version=inputs.schema_version,
            challenge_id=inputs.challenge_id,
            policy_id=policy.policy_id,
            split=inputs.split,
            tier=tier,
            decisions=decisions,
        )
        write_json(output_dir / f"{tier}.json", submission_to_dict(submission))


def _self_check(
    input_path: Path,
    outcomes_path: Path,
    submissions_dir: Path,
    policy_path: Optional[Path],
    report_path: Path,
) -> None:
    inputs = load_input(input_path)
    outcomes = load_outcomes(outcomes_path)
    policy = _load_cli_policy(policy_path)
    submissions = [
        load_submission(submissions_dir / f"{tier}.json") for tier in TIERS
    ]
    report = score_submissions(inputs, outcomes, submissions, policy)
    write_json(report_path, report)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "validate-input":
            inputs = load_input(args.input)
            print(f"OK: {len(inputs.episodes)} episodes ({inputs.split})")
        elif args.command == "always-light":
            _always_light(args.input, args.output_dir, args.policy)
            print(f"OK: {args.output_dir}에 3개 제출 파일을 생성했습니다.")
        elif args.command == "self-check":
            _self_check(
                args.input,
                args.outcomes,
                args.submissions,
                args.policy,
                args.report,
            )
            print(f"OK: 검증 보고서를 {args.report}에 기록했습니다.")
        else:  # pragma: no cover - argparse enforces the command set.
            raise AssertionError(args.command)
    except (OSError, ProtocolError, ScoringError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2
    return 0


def self_check_entry() -> int:
    return main(["self-check", *sys.argv[1:]])


if __name__ == "__main__":
    raise SystemExit(main())
