# SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
# SPDX-License-Identifier: Apache-2.0

"""Build a runtime-check workload from materialized public Train/Dev inputs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from .protocol import InputBatch, load_input


PUBLIC_RUNTIME_PROFILE_ID = "ossp-2026-public-train-dev-runtime-v1"


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label}은 JSON 객체여야 합니다.")
    return value


def _split_registry(
    registry: Mapping[str, Any],
    split: str,
) -> Mapping[str, Any]:
    splits = _require_mapping(registry.get("splits"), "공개 자료 splits")
    return _require_mapping(splits.get(split), f"공개 자료 {split}")


def _validate_materialized_input(
    *,
    path: Path,
    split: str,
    registry: Mapping[str, Any],
) -> InputBatch:
    split_record = _split_registry(registry, split)
    counts = _require_mapping(split_record.get("counts"), f"{split} counts")
    hashes = _require_mapping(split_record.get("sha256"), f"{split} sha256")
    expected_count = counts.get("total_inputs")
    expected_hash = hashes.get("materialized_inputs")
    if not isinstance(expected_count, int) or expected_count < 1:
        raise ValueError(f"{split} 공개 문항 수가 올바르지 않습니다.")
    if not isinstance(expected_hash, str) or len(expected_hash) != 64:
        raise ValueError(f"{split} materialized SHA-256이 올바르지 않습니다.")
    if not path.is_file():
        raise ValueError(
            f"materialized {split} 입력이 없습니다: {path}. "
            "먼저 tools/materialize_public_data.py를 실행하십시오."
        )
    actual_hash = _sha256_file(path)
    if actual_hash != expected_hash:
        raise ValueError(
            f"materialized {split} 입력 SHA-256이 공개 registry와 다릅니다."
        )
    inputs = load_input(path)
    if inputs.split != split:
        raise ValueError(f"materialized 입력의 split이 {split}이 아닙니다.")
    if len(inputs.episodes) != expected_count:
        raise ValueError(f"materialized {split} 문항 수가 registry와 다릅니다.")
    return inputs


def combine_public_inputs(train: InputBatch, dev: InputBatch) -> InputBatch:
    """Combine validated public inputs without outcomes or extra metadata."""

    if train.schema_version != dev.schema_version:
        raise ValueError("Train과 Dev의 schema_version이 다릅니다.")
    if train.challenge_id != dev.challenge_id:
        raise ValueError("Train과 Dev의 challenge_id가 다릅니다.")
    if train.split != "train" or dev.split != "dev":
        raise ValueError("공개 실행 입력은 Train과 Dev 순서로 결합해야 합니다.")
    identifiers = [
        episode.episode_id for episode in (*train.episodes, *dev.episodes)
    ]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("Train과 Dev 사이에 중복 episode_id가 있습니다.")
    return InputBatch(
        schema_version=train.schema_version,
        challenge_id="ossp-2026-public-runtime-check",
        split="public-train-dev",
        episodes=(*train.episodes, *dev.episodes),
    )


def load_public_runtime_workload(
    *,
    train_path: Path,
    dev_path: Path,
    registry_path: Path,
) -> InputBatch:
    """Load pinned public Train/Dev inputs and combine them for runtime checks."""

    try:
        registry_value = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"공개 자료 registry를 읽을 수 없습니다: {exc}") from exc
    registry = _require_mapping(registry_value, "공개 자료 registry")
    train = _validate_materialized_input(
        path=train_path,
        split="train",
        registry=registry,
    )
    dev = _validate_materialized_input(
        path=dev_path,
        split="dev",
        registry=registry,
    )
    expected_challenge_id = registry.get("challenge_id")
    if (
        train.challenge_id != expected_challenge_id
        or dev.challenge_id != expected_challenge_id
    ):
        raise ValueError("materialized 입력의 challenge_id가 registry와 다릅니다.")
    return combine_public_inputs(train, dev)
