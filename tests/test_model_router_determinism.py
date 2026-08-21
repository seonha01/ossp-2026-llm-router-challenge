# SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
# SPDX-License-Identifier: Apache-2.0

"""The router must be a pure function of (prompt text, tier).

The operator re-runs submissions with shuffled inputs, so any dependence on
episode ids, input order, or which other prompts share the batch would be a
disqualifying bug. These tests pin all three properties on the real artifact.
"""

from __future__ import annotations

import pathlib
import random
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ossp_router import model_router  # noqa: E402
from ossp_router.protocol import Episode, load_input  # noqa: E402

_N = 220  # enough to exercise the forked two-worker path (>= 64)


def _load_episodes():
    materialized = ROOT / "data/materialized/dev/inputs.json"
    base = ROOT / "data/dev/inputs-base.json"
    inputs = load_input(materialized if materialized.exists() else base)
    return list(inputs.episodes)[:_N]


def _choices(episodes, tier):
    """episode_id -> model_id for one tier."""
    decisions = model_router.select_decisions(episodes, tier, _ARTIFACT)
    return {decision.episode_id: decision.model_id for decision in decisions}


_EPISODES = None
_ARTIFACT = None


def setUpModule():
    global _EPISODES, _ARTIFACT
    _ARTIFACT = model_router.load_artifact()
    _EPISODES = _load_episodes()


class DeterminismTest(unittest.TestCase):
    def test_order_shuffle_keeps_every_choice(self):
        for tier in ("fast", "balanced", "premium"):
            baseline = _choices(_EPISODES, tier)
            shuffled = list(_EPISODES)
            random.Random(7).shuffle(shuffled)
            self.assertEqual(baseline, _choices(shuffled, tier))

    def test_relabelled_ids_follow_the_prompt(self):
        baseline = _choices(_EPISODES, "premium")
        relabelled = [
            Episode(f"relabel-{index:04d}", episode.prompt, episode.messages)
            for index, episode in enumerate(reversed(_EPISODES))
        ]
        got = _choices(relabelled, "premium")
        for index, episode in enumerate(reversed(_EPISODES)):
            self.assertEqual(
                got[f"relabel-{index:04d}"],
                baseline[episode.episode_id],
                f"prompt of {episode.episode_id} changed model after relabelling",
            )

    def test_choices_do_not_depend_on_the_rest_of_the_batch(self):
        for tier in ("fast", "premium"):
            full = _choices(_EPISODES, tier)
            for size in (40, 100):  # 40 runs serial, 100 runs the forked path
                subset = _EPISODES[:size]
                got = _choices(subset, tier)
                for episode in subset:
                    self.assertEqual(
                        got[episode.episode_id],
                        full[episode.episode_id],
                        f"{episode.episode_id} changed model in a batch of {size}",
                    )

    def test_duplicate_prompts_get_the_same_model(self):
        duplicated = list(_EPISODES)
        for index, episode in enumerate(_EPISODES[:25]):
            duplicated.append(Episode(f"dup-{index:04d}", episode.prompt, episode.messages))
        got = _choices(duplicated, "balanced")
        for index, episode in enumerate(_EPISODES[:25]):
            self.assertEqual(
                got[f"dup-{index:04d}"],
                got[episode.episode_id],
                f"duplicate of {episode.episode_id} routed differently",
            )


if __name__ == "__main__":
    unittest.main()
