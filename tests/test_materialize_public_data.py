# SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
# SPDX-License-Identifier: Apache-2.0

import hashlib
import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "materialize_public_data", ROOT / "tools" / "materialize_public_data.py"
)
assert SPEC is not None and SPEC.loader is not None
MATERIALIZE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MATERIALIZE)


class MaterializePublicDataTests(unittest.TestCase):
    def test_public_data_record_matches_tracked_files(self):
        record = MATERIALIZE._load_json(ROOT / "data" / "public-data.v1.json")
        for split, split_record in record["splits"].items():
            paths = {
                "inputs_base": ROOT / "data" / split / "inputs-base.json",
                "source_fetch_selection": (
                    ROOT / "data" / split / "aime-selection.json"
                ),
                "outcomes": ROOT / "data" / split / "outcomes.json",
            }
            for label, path in paths.items():
                self.assertEqual(
                    split_record["sha256"][label],
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                )

    def test_repository_base_selection_and_outcomes_partition_exactly(self):
        for split, counts in MATERIALIZE.EXPECTED_COUNTS.items():
            base = MATERIALIZE.load_base(split)
            selection = MATERIALIZE.load_aime_selection(split)
            outcomes = MATERIALIZE._load_json(
                ROOT / "data" / split / "outcomes.json"
            )
            base_ids = {row["episode_id"] for row in base["episodes"]}
            selection_ids = {row["episode_id"] for row in selection}
            outcome_ids = {row["episode_id"] for row in outcomes["episodes"]}
            self.assertEqual(counts["base"], len(base_ids))
            self.assertEqual(counts["source_fetch"], len(selection_ids))
            self.assertEqual(counts["full"], len(outcome_ids))
            self.assertFalse(base_ids & selection_ids)
            self.assertEqual(outcome_ids, base_ids | selection_ids)

    def test_public_outcomes_have_only_allowed_content_free_fields(self):
        for split in MATERIALIZE.EXPECTED_COUNTS:
            outcomes = MATERIALIZE._load_json(
                ROOT / "data" / split / "outcomes.json"
            )
            for episode in outcomes["episodes"]:
                self.assertEqual({"episode_id", "models"}, set(episode))
                self.assertEqual(
                    {"ax31-light", "ax31", "axk1-think"},
                    set(episode["models"]),
                )
                for model in episode["models"].values():
                    self.assertEqual(
                        {
                            "score",
                            "num_generations",
                            "input_tokens",
                            "output_tokens",
                        },
                        set(model),
                    )

    def test_selected_prompts_require_matching_hashes(self):
        prompt = "public problem"
        selection = [
            {
                "episode_id": "train-0001",
                "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                "source_id": "aime24-public",
                "source_key": {"source_id": "1", "year": 2024},
            }
        ]
        problems = {"aime24-public": {"1": prompt}}
        self.assertEqual(
            [{"episode_id": "train-0001", "prompt": prompt}],
            MATERIALIZE.selected_aime_episodes(selection, problems),
        )
        selection[0]["prompt_sha256"] = "0" * 64
        with self.assertRaisesRegex(
            MATERIALIZE.MaterializationError, "prompt hash mismatch"
        ):
            MATERIALIZE.selected_aime_episodes(selection, problems)


if __name__ == "__main__":
    unittest.main()
