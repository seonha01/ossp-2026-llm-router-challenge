# SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
# SPDX-License-Identifier: Apache-2.0

"""Validate the six-field repository submission metadata JSON."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Optional, Sequence
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMA = ROOT / "schemas" / "technical-submission.v1.schema.json"
DEFAULT_SUBMISSION = ROOT / "submission-ossp-skt.json"


class ValidationError(ValueError):
    """Raised when the technical submission is not valid."""


def _unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError(f"중복 JSON 키: {key}")
        result[key] = value
    return result


def _load_json(path: Path) -> Any:
    def reject_constant(value: str) -> None:
        raise ValidationError(f"JSON 상수 {value!r}는 허용되지 않습니다.")

    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=reject_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"{path} 파일을 읽을 수 없습니다: {exc}") from exc


def validate_submission(value: Any, schema: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError("기술 제출 정보는 JSON 객체여야 합니다.")
    if not isinstance(schema, dict):
        raise ValidationError("기술 제출 스키마가 JSON 객체가 아닙니다.")

    required = schema.get("required")
    properties = schema.get("properties")
    if not isinstance(required, list) or not isinstance(properties, dict):
        raise ValidationError("기술 제출 스키마의 필드 정의가 올바르지 않습니다.")
    missing = sorted(set(required) - set(value))
    extra = sorted(set(value) - set(properties))
    if missing or extra:
        details = []
        if missing:
            details.append(f"누락={missing}")
        if extra:
            details.append(f"허용되지 않은 필드={extra}")
        raise ValidationError("기술 제출 정보 필드 오류: " + ", ".join(details))

    for name in required:
        rule = properties[name]
        item = value[name]
        if "const" in rule:
            expected = rule["const"]
            if item != expected or (
                isinstance(expected, int) and isinstance(item, bool)
            ):
                raise ValidationError(f"{name} 값이 공식 스키마와 다릅니다.")
        expected_type = rule.get("type")
        if expected_type == "string" and not isinstance(item, str):
            raise ValidationError(f"{name}은(는) 문자열이어야 합니다.")
        if "maxLength" in rule and len(item) > rule["maxLength"]:
            raise ValidationError(
                f"{name}은(는) {rule['maxLength']}자를 넘을 수 없습니다."
            )
        if "pattern" in rule and re.fullmatch(rule["pattern"], item) is None:
            raise ValidationError(f"{name} 형식이 올바르지 않습니다.")
        if "enum" in rule and item not in rule["enum"]:
            raise ValidationError(f"{name} 값이 허용 목록에 없습니다.")

    repository_url = value["repository_url"]
    try:
        parsed_url = urlsplit(repository_url)
    except ValueError as exc:
        raise ValidationError("repository_url은 올바른 HTTPS URL이어야 합니다.") from exc
    if parsed_url.scheme != "https" or not parsed_url.netloc:
        raise ValidationError("repository_url은 올바른 HTTPS URL이어야 합니다.")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="제출 저장소 루트의 submission-ossp-skt.json을 검증합니다."
    )
    parser.add_argument(
        "--file",
        type=Path,
        default=DEFAULT_SUBMISSION,
        help="검증할 파일 경로(기본값: submission-ossp-skt.json)",
    )
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        submission = validate_submission(_load_json(args.file), _load_json(args.schema))
    except (TypeError, ValidationError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2
    print(
        "OK: 기술 제출 정보가 유효합니다 "
        f"({submission['commit_sha']}, {submission['image_digest']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
