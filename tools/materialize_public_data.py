#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
# SPDX-License-Identifier: Apache-2.0
"""Materialize complete public Train/Dev inputs from pinned public sources."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import tempfile
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
FETCH_SPEC = importlib.util.spec_from_file_location(
    "fetch_public_sources", ROOT / "tools" / "fetch_public_sources.py"
)
assert FETCH_SPEC is not None and FETCH_SPEC.loader is not None
FETCH = importlib.util.module_from_spec(FETCH_SPEC)
FETCH_SPEC.loader.exec_module(FETCH)

DEFAULT_CACHE = ROOT / "data" / "cache" / "public-sources"
DEFAULT_OUTPUT = ROOT / "data" / "materialized"
CHALLENGE_ID = "ossp-2026-llm-router-challenge"
EXPECTED_COUNTS = {
    "train": {"base": 1736, "source_fetch": 24, "full": 1760},
    "dev": {"base": 868, "source_fetch": 12, "full": 880},
}
AIME_SOURCE_YEAR = {"aime24-public": 2024, "aime25-public": 2025}


class MaterializationError(RuntimeError):
    """Raised when public data cannot be reproduced safely."""


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MaterializationError(f"cannot read JSON {path}: {exc}") from exc


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _atomic_json(path: Path, value: Any) -> None:
    content = _json_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _validate_episode(episode: Any, label: str) -> dict[str, str]:
    if not isinstance(episode, dict) or set(episode) != {"episode_id", "prompt"}:
        raise MaterializationError(f"{label} must contain only episode_id and prompt")
    episode_id = episode.get("episode_id")
    prompt = episode.get("prompt")
    if not isinstance(episode_id, str) or not episode_id:
        raise MaterializationError(f"{label}.episode_id must be a non-empty string")
    if not isinstance(prompt, str) or not prompt:
        raise MaterializationError(f"{label}.prompt must be a non-empty string")
    return {"episode_id": episode_id, "prompt": prompt}


def load_base(split: str) -> dict[str, Any]:
    base = _load_json(ROOT / "data" / split / "inputs-base.json")
    expected = EXPECTED_COUNTS[split]["base"]
    if (
        not isinstance(base, dict)
        or set(base) != {"schema_version", "challenge_id", "split", "episodes"}
        or base.get("schema_version") != 1
        or base.get("challenge_id") != CHALLENGE_ID
        or base.get("split") != split
        or not isinstance(base.get("episodes"), list)
        or len(base["episodes"]) != expected
    ):
        raise MaterializationError(f"invalid {split} base input")
    episodes = [
        _validate_episode(episode, f"{split}.episodes[{index}]")
        for index, episode in enumerate(base["episodes"])
    ]
    episode_ids = [episode["episode_id"] for episode in episodes]
    if len(episode_ids) != len(set(episode_ids)) or episode_ids != sorted(episode_ids):
        raise MaterializationError(f"{split} base episode IDs must be unique and sorted")
    return {**base, "episodes": episodes}


def load_aime_selection(split: str) -> list[dict[str, Any]]:
    root = _load_json(ROOT / "data" / split / "aime-selection.json")
    expected = EXPECTED_COUNTS[split]["source_fetch"]
    if (
        not isinstance(root, dict)
        or set(root) != {"schema_version", "split", "episodes"}
        or root.get("schema_version") != 1
        or root.get("split") != split
        or not isinstance(root.get("episodes"), list)
        or len(root["episodes"]) != expected
    ):
        raise MaterializationError(f"invalid {split} AIME selection")
    result: list[dict[str, Any]] = []
    for index, row in enumerate(root["episodes"]):
        if not isinstance(row, dict) or set(row) != {
            "episode_id",
            "prompt_sha256",
            "source_id",
            "source_key",
        }:
            raise MaterializationError(f"invalid {split} AIME selection row {index}")
        source_id = row.get("source_id")
        source_key = row.get("source_key")
        if (
            source_id not in AIME_SOURCE_YEAR
            or not isinstance(source_key, dict)
            or set(source_key) != {"source_id", "year"}
            or source_key.get("year") != AIME_SOURCE_YEAR[source_id]
            or not isinstance(source_key.get("source_id"), str)
            or not source_key["source_id"]
        ):
            raise MaterializationError(f"invalid source key in {split} row {index}")
        episode_id = row.get("episode_id")
        prompt_sha256 = row.get("prompt_sha256")
        if (
            not isinstance(episode_id, str)
            or not episode_id.startswith(f"{split}-")
            or not isinstance(prompt_sha256, str)
            or len(prompt_sha256) != 64
            or any(character not in "0123456789abcdef" for character in prompt_sha256)
        ):
            raise MaterializationError(f"invalid selection value in {split} row {index}")
        result.append(row)
    ids = [row["episode_id"] for row in result]
    if len(ids) != len(set(ids)) or ids != sorted(ids):
        raise MaterializationError(f"{split} AIME episode IDs must be unique and sorted")
    return result


def _source_path(
    source: Mapping[str, Any], cache_root: Path, relative_path: str
) -> Path:
    return (
        cache_root
        / str(source["source_id"])
        / str(source["revision"])
        / relative_path
    )


def load_aime_problems(
    sources: Mapping[str, Mapping[str, Any]], cache_root: Path
) -> dict[str, dict[str, str]]:
    """Read only public problem and ID fields; never load answers into output."""
    try:
        import pyarrow.parquet as parquet
    except ImportError as exc:
        raise MaterializationError(
            "pyarrow is required; install data/sources/"
            "requirements-materialize-public-data.txt"
        ) from exc

    aime24 = sources["aime24-public"]
    path24 = _source_path(aime24, cache_root, aime24["files"][0]["path"])
    try:
        table = parquet.read_table(path24, columns=["id", "problem"])
        rows24 = table.to_pylist()
    except Exception as exc:
        raise MaterializationError(f"cannot read pinned AIME 2024 source: {exc}") from exc
    problems24 = {str(row["id"]): row["problem"] for row in rows24}

    aime25 = sources["aime25-public"]
    path25 = _source_path(aime25, cache_root, aime25["files"][0]["path"])
    problems25: dict[str, str] = {}
    try:
        with path25.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                row = json.loads(line)
                source_id = str(row["id"])
                problem = row["problem"]
                if not isinstance(problem, str):
                    raise TypeError("problem is not a string")
                # The pinned JSONL encoded some LaTeX commands as JSON controls.
                problem = problem.replace("\f", "\\f").replace("\t", "\\t")
                if source_id in problems25:
                    raise MaterializationError(f"duplicate AIME 2025 ID: {source_id}")
                problems25[source_id] = problem
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise MaterializationError(f"cannot read pinned AIME 2025 source: {exc}") from exc
    return {"aime24-public": problems24, "aime25-public": problems25}


def selected_aime_episodes(
    selection: Iterable[Mapping[str, Any]],
    problems: Mapping[str, Mapping[str, str]],
) -> list[dict[str, str]]:
    """Resolve selected prompts and require their precommitted hashes."""
    episodes: list[dict[str, str]] = []
    for row in selection:
        source_id = str(row["source_id"])
        source_key = str(row["source_key"]["source_id"])
        problem = problems.get(source_id, {}).get(source_key)
        if problem is None:
            raise MaterializationError(
                f"selected public source row is missing: {source_id}/{source_key}"
            )
        digest = hashlib.sha256(problem.encode("utf-8")).hexdigest()
        if digest != row["prompt_sha256"]:
            raise MaterializationError(
                f"selected prompt hash mismatch: {source_id}/{source_key}"
            )
        episodes.append({"episode_id": str(row["episode_id"]), "prompt": problem})
    return episodes


def join_full_input(
    split: str,
    base: Mapping[str, Any],
    source_fetch_episodes: Iterable[Mapping[str, str]],
) -> dict[str, Any]:
    episodes = list(base["episodes"]) + [dict(row) for row in source_fetch_episodes]
    episodes.sort(key=lambda row: row["episode_id"])
    ids = [row["episode_id"] for row in episodes]
    if len(ids) != EXPECTED_COUNTS[split]["full"] or len(ids) != len(set(ids)):
        raise MaterializationError(f"{split} full input count or uniqueness mismatch")
    outcomes = _load_json(ROOT / "data" / split / "outcomes.json")
    outcome_ids = [row.get("episode_id") for row in outcomes.get("episodes", [])]
    if ids != sorted(outcome_ids):
        raise MaterializationError(f"{split} input/outcome episode IDs do not match")
    return {
        "challenge_id": CHALLENGE_ID,
        "episodes": episodes,
        "schema_version": 1,
        "split": split,
    }


def materialize(
    splits: Iterable[str],
    cache_root: Path,
    output_root: Path,
    *,
    fetch: bool = True,
    fetcher: Callable[..., Any] = FETCH.fetch_source,
) -> list[tuple[str, Path, int]]:
    sources = FETCH.load_source_pins(FETCH.DEFAULT_PINS)
    selected_sources = {source_id: sources[source_id] for source_id in AIME_SOURCE_YEAR}
    if fetch:
        for source in selected_sources.values():
            fetcher(source, cache_root)
    else:
        for source in selected_sources.values():
            for file_record in source["files"]:
                path = _source_path(source, cache_root, file_record["path"])
                if not path.is_file() or FETCH.sha256_file(path) != file_record["sha256"]:
                    raise MaterializationError(f"missing verified cached source: {path}")
    problems = load_aime_problems(selected_sources, cache_root)
    public_record = _load_json(ROOT / "data" / "public-data.v1.json")
    results = []
    for split in splits:
        base = load_base(split)
        selection = load_aime_selection(split)
        fetched = selected_aime_episodes(selection, problems)
        full = join_full_input(split, base, fetched)
        actual_sha256 = hashlib.sha256(_json_bytes(full)).hexdigest()
        try:
            expected_sha256 = public_record["splits"][split]["sha256"][
                "materialized_inputs"
            ]
        except (KeyError, TypeError) as exc:
            raise MaterializationError("invalid public data hash record") from exc
        if actual_sha256 != expected_sha256:
            raise MaterializationError(
                f"{split} materialized input hash mismatch: "
                f"expected {expected_sha256}, got {actual_sha256}"
            )
        destination = output_root / split / "inputs.json"
        _atomic_json(destination, full)
        results.append((split, destination, len(full["episodes"])))
    return results


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--split", action="append", choices=sorted(EXPECTED_COUNTS), default=[]
    )
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--offline",
        action="store_true",
        help="use already verified cache files without network access",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    splits = args.split or ["train", "dev"]
    try:
        results = materialize(
            splits,
            args.cache_dir.resolve(),
            args.output_dir.resolve(),
            fetch=not args.offline,
        )
    except (MaterializationError, FETCH.SourceFetchError) as exc:
        print(f"error: {exc}", file=os.sys.stderr)
        return 1
    for split, destination, count in results:
        print(f"{split}: {count} episodes: {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
