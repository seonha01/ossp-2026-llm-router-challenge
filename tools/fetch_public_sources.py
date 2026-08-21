#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
# SPDX-License-Identifier: Apache-2.0
"""Fetch and verify pinned public Train/Dev source files."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping, Optional


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PINS = ROOT / "data" / "sources" / "source-pins.v1.json"
DEFAULT_CACHE = ROOT / "data" / "cache" / "public-sources"
CHUNK_SIZE = 1024 * 1024


class SourceFetchError(ValueError):
    """Raised when a source record or fetched artifact is invalid."""


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise SourceFetchError(f"{label} must be a non-empty string")
    return value


def _sha256_text(value: Any, label: str) -> str:
    text = _text(value, label)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise SourceFetchError(f"{label} must be a lowercase SHA-256 digest")
    return text


def _safe_path(value: Any, label: str) -> PurePosixPath:
    text = _text(value, label)
    path = PurePosixPath(text)
    if (
        not path.parts
        or path.is_absolute()
        or any(part in ("", ".", "..") for part in path.parts)
    ):
        raise SourceFetchError(f"{label} must be a safe relative POSIX path")
    return path


def load_source_pins(path: Path) -> dict[str, Mapping[str, Any]]:
    try:
        root = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SourceFetchError(f"cannot read source pins {path}: {exc}") from exc
    if not isinstance(root, dict) or root.get("schema_version") != 1:
        raise SourceFetchError("source pins must use schema_version 1")
    sources = root.get("sources")
    if not isinstance(sources, list) or not sources:
        raise SourceFetchError("source pins must contain a non-empty sources list")
    result: dict[str, Mapping[str, Any]] = {}
    for index, source in enumerate(sources):
        if not isinstance(source, dict):
            raise SourceFetchError(f"sources[{index}] must be an object")
        source_id = _text(source.get("source_id"), f"sources[{index}].source_id")
        if source_id in result:
            raise SourceFetchError(f"duplicate source_id: {source_id}")
        result[source_id] = source
    return result


def source_file_url(source: Mapping[str, Any], file_record: Mapping[str, Any]) -> str:
    explicit = file_record.get("url")
    if explicit is not None:
        url = _text(explicit, "file.url")
    elif source.get("source_type") == "huggingface":
        repo_id = _text(source.get("repo_id"), "source.repo_id")
        revision = _text(source.get("revision"), "source.revision")
        path = _safe_path(file_record.get("path"), "file.path")
        quoted_repo = urllib.parse.quote(repo_id, safe="/")
        quoted_revision = urllib.parse.quote(revision, safe="")
        quoted_path = urllib.parse.quote(path.as_posix(), safe="/")
        url = (
            f"https://huggingface.co/datasets/{quoted_repo}/resolve/"
            f"{quoted_revision}/{quoted_path}"
        )
    else:
        raise SourceFetchError("file.url is required for this source type")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise SourceFetchError("source URLs must use absolute HTTPS URLs")
    return url


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download_verified(
    url: str,
    destination: Path,
    expected_sha256: str,
    *,
    opener: Callable[..., Any],
) -> str:
    if destination.is_file() and sha256_file(destination) == expected_sha256:
        return "cached"
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            digest = hashlib.sha256()
            with opener(url, timeout=60) as response:
                while True:
                    chunk = response.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    digest.update(chunk)
                    temporary.write(chunk)
            temporary.flush()
            os.fsync(temporary.fileno())
        actual_sha256 = digest.hexdigest()
        if actual_sha256 != expected_sha256:
            raise SourceFetchError(
                f"SHA-256 mismatch for {url}: expected {expected_sha256}, "
                f"got {actual_sha256}"
            )
        os.replace(temporary_path, destination)
        temporary_path = None
        return "downloaded"
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def fetch_source(
    source: Mapping[str, Any],
    cache_root: Path,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> list[tuple[Path, str]]:
    source_id = _text(source.get("source_id"), "source.source_id")
    revision = _text(source.get("revision"), "source.revision")
    files = source.get("files")
    if not isinstance(files, list) or not files:
        raise SourceFetchError(f"source {source_id} has no directly fetchable files")
    fetched: list[tuple[Path, str]] = []
    for index, file_record in enumerate(files):
        if not isinstance(file_record, dict):
            raise SourceFetchError(f"source {source_id} files[{index}] must be an object")
        relative = _safe_path(file_record.get("path"), f"{source_id}.files[{index}].path")
        expected = _sha256_text(
            file_record.get("sha256"), f"{source_id}.files[{index}].sha256"
        )
        destination = cache_root / source_id / revision / Path(*relative.parts)
        status = _download_verified(
            source_file_url(source, file_record),
            destination,
            expected,
            opener=opener,
        )
        fetched.append((destination, status))
    return fetched


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pins", type=Path, default=DEFAULT_PINS)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument(
        "--source",
        action="append",
        default=[],
        help="source_id to fetch; repeat this option to fetch more than one source",
    )
    parser.add_argument("--list", action="store_true", help="list pinned source IDs")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    sources = load_source_pins(args.pins)
    if args.list:
        for source_id in sources:
            print(source_id)
    if not args.source:
        if args.list:
            return 0
        parser.error("at least one --source is required unless --list is used")
    unknown = sorted(set(args.source) - set(sources))
    if unknown:
        parser.error(f"unknown source_id: {', '.join(unknown)}")
    for source_id in args.source:
        for path, status in fetch_source(sources[source_id], args.cache_dir):
            print(f"{status}: {source_id}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
