# SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
# SPDX-License-Identifier: Apache-2.0

"""Trusted helper for retaining and extracting a quota-backed output volume."""

from __future__ import annotations

import argparse
import os
import stat
import sys
import time
from pathlib import Path
from typing import Optional, Sequence


PARTICIPANT_OUTPUT_ERROR = 2
OPERATOR_IO_ERROR = 3


def _extract(source: Path, destination: Path, maximum: int) -> int:
    try:
        entries = list(os.scandir(source))
    except PermissionError as exc:
        print(
            f"참가자 출력 공간의 권한으로 내용을 검사할 수 없음: {exc}",
            file=sys.stderr,
        )
        return PARTICIPANT_OUTPUT_ERROR
    except OSError as exc:
        print(f"운영자 출력 볼륨을 읽을 수 없음: {exc}", file=sys.stderr)
        return OPERATOR_IO_ERROR
    if len(entries) != 1 or entries[0].name != "submission.json":
        names = sorted(entry.name for entry in entries)
        print(
            "출력 공간에는 submission.json 하나만 있어야 합니다: "
            f"{names}",
            file=sys.stderr,
        )
        return PARTICIPANT_OUTPUT_ERROR

    try:
        before_open = entries[0].stat(follow_symlinks=False)
    except OSError as exc:
        print(f"submission.json의 상태를 확인할 수 없음: {exc}", file=sys.stderr)
        return PARTICIPANT_OUTPUT_ERROR
    if not stat.S_ISREG(before_open.st_mode) or before_open.st_nlink != 1:
        print("submission.json은 일반 파일이어야 합니다.", file=sys.stderr)
        return PARTICIPANT_OUTPUT_ERROR
    if before_open.st_size > maximum:
        print(
            f"submission.json이 {maximum}바이트 한도를 넘었습니다.",
            file=sys.stderr,
        )
        return PARTICIPANT_OUTPUT_ERROR

    output = source / "submission.json"
    flags = os.O_RDONLY | os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(output, flags)
    except OSError as exc:
        print(f"submission.json을 안전하게 열 수 없음: {exc}", file=sys.stderr)
        return PARTICIPANT_OUTPUT_ERROR
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_dev != before_open.st_dev
            or metadata.st_ino != before_open.st_ino
        ):
            print("submission.json은 일반 파일이어야 합니다.", file=sys.stderr)
            return PARTICIPANT_OUTPUT_ERROR
        if metadata.st_size > maximum:
            print(
                f"submission.json이 {maximum}바이트 한도를 넘었습니다.",
                file=sys.stderr,
            )
            return PARTICIPANT_OUTPUT_ERROR
        chunks = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > maximum:
            print(
                f"submission.json이 {maximum}바이트 한도를 넘었습니다.",
                file=sys.stderr,
            )
            return PARTICIPANT_OUTPUT_ERROR
        after = os.fstat(descriptor)
        if (
            after.st_ino != metadata.st_ino
            or after.st_size != metadata.st_size
            or after.st_mtime_ns != metadata.st_mtime_ns
        ):
            print("submission.json이 검사 중 변경되었습니다.", file=sys.stderr)
            return PARTICIPANT_OUTPUT_ERROR
    except OSError as exc:
        print(f"submission.json을 읽을 수 없음: {exc}", file=sys.stderr)
        return OPERATOR_IO_ERROR
    finally:
        os.close(descriptor)

    temporary = destination.with_name(f".{destination.name}.operator-tmp")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o644)
        os.replace(temporary, destination)
    except OSError as exc:
        print(f"운영자 결과 파일을 쓸 수 없음: {exc}", file=sys.stderr)
        return OPERATOR_IO_ERROR
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("hold")
    extract = subparsers.add_parser("extract")
    extract.add_argument("--source", type=Path, required=True)
    extract.add_argument("--destination", type=Path, required=True)
    extract.add_argument("--maximum", type=int, required=True)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "hold":
        while True:
            time.sleep(3600)
    if args.maximum < 0:
        print("--maximum은 0 이상이어야 합니다.", file=sys.stderr)
        return OPERATOR_IO_ERROR
    return _extract(args.source, args.destination, args.maximum)


if __name__ == "__main__":
    raise SystemExit(main())
