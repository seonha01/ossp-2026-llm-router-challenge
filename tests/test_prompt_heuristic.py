# SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

from ossp_router.heuristic import (
    episode_text,
    extract_features,
    make_submission,
)
from ossp_router.protocol import ProtocolError, load_bundled_policy, parse_input


ROOT = pathlib.Path(__file__).resolve().parents[1]


def _batch(episode_ids=("first", "second", "third")):
    return parse_input(
        {
            "schema_version": 1,
            "challenge_id": "phase-c-test",
            "split": "synthetic",
            "episodes": [
                {
                    "episode_id": episode_ids[0],
                    "prompt": "오늘 날씨를 짧게 설명해 주세요.",
                },
                {
                    "episode_id": episode_ids[1],
                    "prompt": (
                        "Prove x^2 + 2*x + 1 = (x+1)^2 and derive every step. "
                        "Numbers: 12, 24, 48, 96."
                    ),
                },
                {
                    "episode_id": episode_ids[2],
                    "messages": [
                        {"role": "system", "content": "Write valid Python."},
                        {
                            "role": "user",
                            "content": "```python\ndef solve(values):\n    return values\n```",
                        },
                        {"role": "assistant", "content": "Explain complexity."},
                    ],
                },
            ],
        }
    )


def _content_decisions(batch, submission):
    model_by_id = {
        decision.episode_id: decision.model_id
        for decision in submission.decisions
    }
    return {
        episode_text(episode): model_by_id[episode.episode_id]
        for episode in batch.episodes
    }


class PromptHeuristicTest(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = load_bundled_policy()

    def test_order_does_not_change_content_decisions(self) -> None:
        original = _batch()
        reversed_batch = parse_input(
            {
                "schema_version": original.schema_version,
                "challenge_id": original.challenge_id,
                "split": original.split,
                "episodes": [
                    {
                        "episode_id": episode.episode_id,
                        **(
                            {"prompt": episode.prompt}
                            if episode.prompt is not None
                            else {
                                "messages": [
                                    {"role": message.role, "content": message.content}
                                    for message in episode.messages or ()
                                ]
                            }
                        ),
                    }
                    for episode in reversed(original.episodes)
                ],
            }
        )
        left = make_submission(original, self.policy, "premium")
        right = make_submission(reversed_batch, self.policy, "premium")
        self.assertEqual(
            _content_decisions(original, left),
            _content_decisions(reversed_batch, right),
        )

    def test_changed_ids_do_not_change_content_decisions(self) -> None:
        original = _batch()
        changed = _batch(("opaque-a", "opaque-b", "opaque-c"))
        for tier in ("fast", "balanced", "premium"):
            with self.subTest(tier=tier):
                self.assertEqual(
                    _content_decisions(
                        original,
                        make_submission(original, self.policy, tier),
                    ),
                    _content_decisions(
                        changed,
                        make_submission(changed, self.policy, tier),
                    ),
                )

    def test_tiers_use_conservative_monotonic_model_choices(self) -> None:
        batch = _batch()
        self.assertEqual(
            {
                "fast": ["ax31-light", "ax31", "ax31"],
                "balanced": ["ax31-light", "ax31", "ax31"],
                "premium": ["ax31", "ax31", "ax31"],
            },
            {
                tier: [
                    decision.model_id
                    for decision in make_submission(
                        batch,
                        self.policy,
                        tier,
                    ).decisions
                ]
                for tier in ("fast", "balanced", "premium")
            },
        )

    def test_same_input_and_tier_are_byte_deterministic(self) -> None:
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(ROOT / "src")
        with tempfile.TemporaryDirectory() as temporary:
            target = pathlib.Path(temporary)
            outputs = []
            for index in range(2):
                output = target / f"submission-{index}.json"
                result = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / "baselines/prompt_heuristic.py"),
                        "--input",
                        str(ROOT / "data/toy/inputs.json"),
                        "--tier",
                        "balanced",
                        "--output",
                        str(output),
                    ],
                    cwd=ROOT,
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(0, result.returncode, result.stderr)
                outputs.append(output.read_bytes())
            self.assertEqual(outputs[0], outputs[1])
            self.assertEqual("balanced", json.loads(outputs[0])["tier"])

    def test_features_are_content_only_and_cover_documented_examples(self) -> None:
        batch = _batch()
        korean = extract_features(batch.episodes[0])
        math = extract_features(batch.episodes[1])
        code = extract_features(batch.episodes[2])
        self.assertGreater(korean.hangul_ratio, 0)
        self.assertGreaterEqual(math.math_marker_count, 2)
        self.assertGreater(code.code_marker_count, 0)
        self.assertEqual(3, code.message_count)

    def test_runtime_input_rejects_non_protocol_metadata(self) -> None:
        for forbidden in ("task_name", "source", "outcomes"):
            with self.subTest(field=forbidden):
                value = {
                    "schema_version": 1,
                    "challenge_id": "phase-c-test",
                    "split": "synthetic",
                    "episodes": [
                        {
                            "episode_id": "opaque",
                            "prompt": "hello",
                            forbidden: "not allowed",
                        }
                    ],
                }
                with self.assertRaises(ProtocolError):
                    parse_input(value)


if __name__ == "__main__":
    unittest.main()
