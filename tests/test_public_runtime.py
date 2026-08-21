# SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import json
import pathlib
import tempfile
import unittest

from ossp_router.protocol import dumps_json, parse_input, write_json
from ossp_router.public_runtime import (
    PUBLIC_RUNTIME_PROFILE_ID,
    combine_public_inputs,
    load_public_runtime_workload,
)


class PublicRuntimeWorkloadTest(unittest.TestCase):
    def _batch(self, split: str, identifier: str):
        return parse_input(
            {
                "schema_version": 1,
                "challenge_id": "challenge",
                "split": split,
                "episodes": [
                    {"episode_id": identifier, "prompt": f"prompt-{identifier}"}
                ],
            }
        )

    def test_combines_train_then_dev_without_outcomes(self) -> None:
        combined = combine_public_inputs(
            self._batch("train", "train-1"),
            self._batch("dev", "dev-1"),
        )
        self.assertEqual("public-train-dev", combined.split)
        self.assertEqual(
            ["train-1", "dev-1"],
            [episode.episode_id for episode in combined.episodes],
        )
        encoded = dumps_json(
            {
                "schema_version": combined.schema_version,
                "challenge_id": combined.challenge_id,
                "split": combined.split,
                "episodes": [
                    {
                        "episode_id": episode.episode_id,
                        "prompt": episode.prompt,
                    }
                    for episode in combined.episodes
                ],
            }
        )
        self.assertNotIn("outcome", encoded)
        self.assertNotIn("score", encoded)

    def test_rejects_duplicate_ids_across_public_splits(self) -> None:
        with self.assertRaisesRegex(ValueError, "중복 episode_id"):
            combine_public_inputs(
                self._batch("train", "same"),
                self._batch("dev", "same"),
            )

    def test_loader_requires_registry_hashes_and_counts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = pathlib.Path(temporary)
            train_path = target / "train.json"
            dev_path = target / "dev.json"
            registry_path = target / "registry.json"
            train_value = {
                "schema_version": 1,
                "challenge_id": "challenge",
                "split": "train",
                "episodes": [{"episode_id": "train-1", "prompt": "train"}],
            }
            dev_value = {
                "schema_version": 1,
                "challenge_id": "challenge",
                "split": "dev",
                "episodes": [{"episode_id": "dev-1", "prompt": "dev"}],
            }
            write_json(train_path, train_value)
            write_json(dev_path, dev_value)
            registry = {
                "schema_version": 1,
                "challenge_id": "challenge",
                "splits": {
                    "train": {
                        "counts": {"total_inputs": 1},
                        "sha256": {
                            "materialized_inputs": hashlib.sha256(
                                train_path.read_bytes()
                            ).hexdigest()
                        },
                    },
                    "dev": {
                        "counts": {"total_inputs": 1},
                        "sha256": {
                            "materialized_inputs": hashlib.sha256(
                                dev_path.read_bytes()
                            ).hexdigest()
                        },
                    },
                },
            }
            registry_path.write_text(json.dumps(registry), encoding="utf-8")
            combined = load_public_runtime_workload(
                train_path=train_path,
                dev_path=dev_path,
                registry_path=registry_path,
            )
            self.assertEqual(2, len(combined.episodes))
            train_path.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                load_public_runtime_workload(
                    train_path=train_path,
                    dev_path=dev_path,
                    registry_path=registry_path,
                )

    def test_profile_id_is_public_train_dev(self) -> None:
        self.assertEqual(
            "ossp-2026-public-train-dev-runtime-v1",
            PUBLIC_RUNTIME_PROFILE_ID,
        )


if __name__ == "__main__":
    unittest.main()
