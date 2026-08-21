#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
# SPDX-License-Identifier: Apache-2.0
"""Reproduce and verify the pinned DeepMind Mathematics reference pool.

This recipe imports a separately checked-out upstream repository. It does not
copy or modify upstream source files. Reference TSV files are written only
after both complete 900-row regime hashes match the pinned values.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import operator
import random
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any, NamedTuple


UPSTREAM_REVISION = "427f45075f84b8b9774950196ad63867ca20ffb3"
ROOT_SEED = 220302
SAMPLES_PER_REGIME = 900
MAX_DUPLICATE_RETRY = 100
MAX_GENERATION_ATTEMPTS = 1000
MAX_QUESTION_LENGTH = 160
MAX_ANSWER_LENGTH = 30
EXPECTED_SHA256 = {
    "interpolate": "ede42cb2ae748e4929d216a8807eb4676bfce8a53d9343eba7c7d73964766408",
    "extrapolate": "d5c4b497f5472e23dbdff369f30d6e4c608a3ea997a2b387d375c6759b92b75a",
}
EXPECTED_PUBLIC_COUNTS = {"train": 303, "dev": 153}
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SELECTION = (
    REPOSITORY_ROOT
    / "data"
    / "sources"
    / "deepmind-mathematics-selection.v1.json"
)


class ReproductionError(RuntimeError):
    """Raised when the pinned source cannot be reproduced exactly."""


class Sample(NamedTuple):
    sample_id: str
    problem: str
    answer: str


def generation_seed(
    regime: str,
    module_name: str,
    module_sample_index: int,
    duplicate_retry: int,
) -> int:
    """Derive the documented unsigned 32-bit generation seed."""
    fields = (
        UPSTREAM_REVISION,
        str(ROOT_SEED),
        regime,
        module_name,
        str(module_sample_index),
        str(duplicate_retry),
    )
    payload = "\0".join(fields).encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:4], "big")


def flatten_modules(tree: Mapping[str, Any]) -> dict[str, Callable[[], Any]]:
    """Flatten nested category/module mappings and sort their full names."""
    flat: dict[str, Callable[[], Any]] = {}

    def visit(node: Mapping[str, Any], prefix: str | None = None) -> None:
        for key, value in node.items():
            if not isinstance(key, str) or not key:
                raise ReproductionError("module keys must be non-empty strings")
            full_name = f"{prefix}__{key}" if prefix else key
            if isinstance(value, Mapping):
                visit(value, full_name)
            elif callable(value):
                if full_name in flat:
                    raise ReproductionError(f"duplicate module name: {full_name}")
                flat[full_name] = value
            else:
                raise ReproductionError(f"invalid module entry: {full_name}")

    visit(tree)
    return {name: flat[name] for name in sorted(flat)}


@contextlib.contextmanager
def isolated_rng_state(seed: int, numpy: Any, sympy_rng: Any) -> Iterator[None]:
    """Seed all three generators for one sample and restore prior states."""
    python_state = random.getstate()
    numpy_state = numpy.random.get_state()
    sympy_state = sympy_rng.getstate()
    try:
        random.seed(seed)
        numpy.random.seed(seed)
        sympy_rng.seed(seed)
        yield
    finally:
        sympy_rng.setstate(sympy_state)
        numpy.random.set_state(numpy_state)
        random.setstate(python_state)


@contextlib.contextmanager
def upstream_compatibility(numpy: Any, composition: Any) -> Iterator[None]:
    """Apply deterministic modern-runtime compatibility without source edits."""
    original_zeros = numpy.zeros
    original_empty = numpy.empty
    original_randint = random.randint
    original_pop = composition.Context.pop
    original_expand_entities = composition.expand_entities

    class ItemsetArray(numpy.ndarray):
        """NumPy 2 array view retaining the two-argument itemset operation."""

        def itemset(self, *args: Any) -> None:
            if len(args) == 2:
                self[args[0]] = args[1]
            elif len(args) == 1:
                self.flat[0] = args[0]
            else:
                raise TypeError("itemset expects one or two arguments")

    def with_itemset(array: Any) -> Any:
        if isinstance(array, numpy.ndarray) and not isinstance(array, ItemsetArray):
            return array.view(ItemsetArray)
        return array

    def compatible_zeros(*args: Any, **kwargs: Any) -> Any:
        return with_itemset(original_zeros(*args, **kwargs))

    def compatible_empty(*args: Any, **kwargs: Any) -> Any:
        return with_itemset(original_empty(*args, **kwargs))

    def integer_bound(value: Any) -> int:
        try:
            return operator.index(value)
        except TypeError:
            integer = int(value)
            if value != integer:
                raise
            return integer

    def compatible_randint(lower: Any, upper: Any) -> int:
        return original_randint(integer_bound(lower), integer_bound(upper))

    def deterministic_pop(context: Any) -> str:
        allowed = (
            composition._ALLOWED_SYMBOLS
            .difference(context._relation_symbols)
            .difference(context._self_symbols)
            .difference(context._child_symbols)
        )
        if not allowed:
            raise ValueError("Ran out of symbols")
        symbol = random.choice(sorted(allowed))
        context._self_symbols.add(symbol)
        return symbol

    def deterministic_expand_entities(context: Any, **kwargs: Any) -> Any:
        new_kwargs = kwargs.copy()
        entities = list(context.child_entities)
        for key, maybe_entity in kwargs.items():
            if isinstance(maybe_entity, composition.Entity):
                if maybe_entity not in entities:
                    entities.append(maybe_entity)
                new_kwargs[key] = maybe_entity.handle
        random.shuffle(entities)
        child_descriptions: list[str] = []
        for entity in entities:
            child_descriptions.append(entity.child_description)
            if not entity.expression_used:
                child_descriptions.append(entity.description)
        child_description = " ".join(text for text in child_descriptions if text)
        return child_description, new_kwargs

    numpy.zeros = compatible_zeros
    numpy.empty = compatible_empty
    random.randint = compatible_randint
    composition.Context.pop = deterministic_pop
    composition.expand_entities = deterministic_expand_entities
    try:
        yield
    finally:
        composition.expand_entities = original_expand_entities
        composition.Context.pop = original_pop
        random.randint = original_randint
        numpy.empty = original_empty
        numpy.zeros = original_zeros


def _validated_text(value: Any, label: str) -> str:
    text = str(value)
    if not text:
        raise ReproductionError(f"empty {label}")
    if any(character in text for character in ("\t", "\n", "\r")):
        raise ReproductionError(f"{label} contains a TSV delimiter")
    return text


def generate_regime(
    regime: str,
    module_tree: Mapping[str, Any],
    numpy: Any,
    sympy_rng: Any,
) -> list[Sample]:
    """Generate one complete length-bounded, question-unique regime."""
    modules = flatten_modules(module_tree)
    names = list(modules)
    if not names:
        raise ReproductionError(f"regime {regime} has no modules")
    seen_questions: set[str] = set()
    samples: list[Sample] = []
    for sample_index in range(SAMPLES_PER_REGIME):
        module_name = names[sample_index % len(names)]
        module_sample_index = sample_index // len(names)
        module = modules[module_name]
        for duplicate_retry in range(MAX_DUPLICATE_RETRY + 1):
            seed = generation_seed(
                regime, module_name, module_sample_index, duplicate_retry
            )
            with isolated_rng_state(seed, numpy, sympy_rng):
                for _generation_attempt in range(MAX_GENERATION_ATTEMPTS):
                    generated = module()
                    problem = _validated_text(generated.question, "problem")
                    answer = _validated_text(generated.answer, "answer")
                    if (
                        len(problem) <= MAX_QUESTION_LENGTH
                        and len(answer) <= MAX_ANSWER_LENGTH
                    ):
                        break
                else:
                    raise ReproductionError(
                        f"no length-bounded sample after "
                        f"{MAX_GENERATION_ATTEMPTS} attempts: "
                        f"{regime}/{module_name}/{module_sample_index}/"
                        f"retry-{duplicate_retry}"
                    )
            if problem in seen_questions:
                continue
            seen_questions.add(problem)
            samples.append(
                Sample(
                    sample_id=(
                        f"{regime}:{module_name}:{module_sample_index}:{seed:08x}"
                    ),
                    problem=problem,
                    answer=answer,
                )
            )
            break
        else:
            raise ReproductionError(
                f"no acceptable sample after {MAX_DUPLICATE_RETRY + 1} attempts: "
                f"{regime}/{module_name}/{module_sample_index}"
            )
    return samples


def reference_bytes(samples: list[Sample]) -> bytes:
    """Serialize reference rows with LF separators and no final LF."""
    rows = [f"{row.sample_id}\t{row.problem}\t{row.answer}" for row in samples]
    return "\n".join(rows).encode("utf-8")


def verify_reference(samples_by_regime: Mapping[str, list[Sample]]) -> dict[str, str]:
    """Require the exact row count and SHA-256 for both pinned regimes."""
    actual: dict[str, str] = {}
    for regime, expected in EXPECTED_SHA256.items():
        samples = samples_by_regime.get(regime)
        if samples is None or len(samples) != SAMPLES_PER_REGIME:
            count = 0 if samples is None else len(samples)
            raise ReproductionError(
                f"{regime} row count mismatch: expected {SAMPLES_PER_REGIME}, got {count}"
            )
        digest = hashlib.sha256(reference_bytes(samples)).hexdigest()
        actual[regime] = digest
        if digest != expected:
            raise ReproductionError(
                f"{regime} SHA-256 mismatch: expected {expected}, got {digest}"
            )
    return actual


def load_public_selection(path: Path) -> dict[str, list[dict[str, str]]]:
    """Load the frozen public episode/sample mapping without private metadata."""
    try:
        root = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReproductionError(f"cannot read public selection {path}: {exc}") from exc
    if not isinstance(root, dict) or root.get("schema_version") != 1:
        raise ReproductionError("public selection must use schema_version 1")
    if root.get("source_id") != "deepmind-mathematics":
        raise ReproductionError("public selection has the wrong source_id")
    if root.get("reference_sha256") != EXPECTED_SHA256:
        raise ReproductionError("public selection reference hashes are not pinned values")
    splits = root.get("splits")
    if not isinstance(splits, dict) or set(splits) != set(EXPECTED_PUBLIC_COUNTS):
        raise ReproductionError("public selection must contain train and dev splits")
    result: dict[str, list[dict[str, str]]] = {}
    episode_ids: set[str] = set()
    sample_ids: set[str] = set()
    for split, expected_count in EXPECTED_PUBLIC_COUNTS.items():
        rows = splits.get(split)
        if not isinstance(rows, list) or len(rows) != expected_count:
            count = -1 if not isinstance(rows, list) else len(rows)
            raise ReproductionError(
                f"public {split} selection count mismatch: "
                f"expected {expected_count}, got {count}"
            )
        validated: list[dict[str, str]] = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or set(row) != {"episode_id", "sample_id"}:
                raise ReproductionError(f"invalid public selection row: {split}[{index}]")
            episode_id = row.get("episode_id")
            sample_id = row.get("sample_id")
            if (
                not isinstance(episode_id, str)
                or not episode_id.startswith(f"{split}-")
                or not isinstance(sample_id, str)
                or not sample_id
            ):
                raise ReproductionError(f"invalid public selection value: {split}[{index}]")
            if episode_id in episode_ids or sample_id in sample_ids:
                raise ReproductionError("public selection IDs must be globally unique")
            episode_ids.add(episode_id)
            sample_ids.add(sample_id)
            validated.append({"episode_id": episode_id, "sample_id": sample_id})
        if [row["episode_id"] for row in validated] != sorted(
            row["episode_id"] for row in validated
        ):
            raise ReproductionError(f"public {split} selection must sort episode_id")
        result[split] = validated
    return result


def public_fragments(
    samples_by_regime: Mapping[str, list[Sample]],
    selection: Mapping[str, list[dict[str, str]]],
) -> dict[str, dict[str, Any]]:
    """Join verified references to public IDs while omitting gold answers."""
    samples_by_id = {
        sample.sample_id: sample
        for samples in samples_by_regime.values()
        for sample in samples
    }
    fragments: dict[str, dict[str, Any]] = {}
    for split in EXPECTED_PUBLIC_COUNTS:
        episodes = []
        for row in selection[split]:
            sample = samples_by_id.get(row["sample_id"])
            if sample is None:
                raise ReproductionError(
                    f"selected sample_id is absent from verified references: "
                    f"{row['sample_id']}"
                )
            episodes.append(
                {"episode_id": row["episode_id"], "prompt": sample.problem}
            )
        fragments[split] = {
            "schema_version": 1,
            "source_id": "deepmind-mathematics",
            "split": split,
            "episodes": episodes,
        }
    return fragments


def verify_checkout(source_dir: Path) -> None:
    """Fail unless source_dir is the exact pinned Git revision."""
    try:
        result = subprocess.run(
            ["git", "-C", os.fspath(source_dir), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ReproductionError(f"cannot inspect upstream checkout: {exc}") from exc
    revision = result.stdout.strip()
    if revision != UPSTREAM_REVISION:
        raise ReproductionError(
            f"upstream revision mismatch: expected {UPSTREAM_REVISION}, got {revision}"
        )


def load_upstream(source_dir: Path) -> tuple[Any, Any, Any, Any]:
    """Import the pinned generator and its RNG dependencies."""
    verify_checkout(source_dir)
    sys.path.insert(0, os.fspath(source_dir))
    try:
        import numpy
        from mathematics_dataset.modules import modules
        from mathematics_dataset.util import composition
        from sympy.core.random import rng as sympy_rng
    except ImportError as exc:
        raise ReproductionError(
            "missing generator dependency; install data/sources/"
            "requirements-deepmind-mathematics.txt"
        ) from exc
    finally:
        try:
            sys.path.remove(os.fspath(source_dir))
        except ValueError:
            pass
    return numpy, modules, composition, sympy_rng


def _atomic_write(path: Path, content: bytes) -> None:
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
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def reproduce(
    source_dir: Path,
    output_dir: Path,
    selection_path: Path = DEFAULT_SELECTION,
) -> dict[str, str]:
    """Generate in memory, verify both hashes, then write local references."""
    numpy, modules, composition, sympy_rng = load_upstream(source_dir)
    with upstream_compatibility(numpy, composition):
        samples_by_regime = {
            "interpolate": generate_regime(
                "interpolate", modules.test(), numpy, sympy_rng
            ),
            "extrapolate": generate_regime(
                "extrapolate", modules.test_extra(), numpy, sympy_rng
            ),
        }
    actual = verify_reference(samples_by_regime)
    selection = load_public_selection(selection_path)
    fragments = public_fragments(samples_by_regime, selection)
    for regime, samples in samples_by_regime.items():
        _atomic_write(output_dir / f"{regime}.tsv", reference_bytes(samples))
    for split, fragment in fragments.items():
        _atomic_write(
            output_dir / f"{split}-fragment.json",
            (json.dumps(fragment, ensure_ascii=False, indent=2) + "\n").encode(
                "utf-8"
            ),
        )
    verification = {
        "schema_version": 1,
        "upstream_revision": UPSTREAM_REVISION,
        "root_seed": ROOT_SEED,
        "samples_per_regime": SAMPLES_PER_REGIME,
        "sha256": actual,
    }
    _atomic_write(
        output_dir / "verification.json",
        (json.dumps(verification, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    return actual


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-dir",
        required=True,
        type=Path,
        help="separate checkout of google-deepmind/mathematics_dataset",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/materialized/deepmind-mathematics"),
        help="local ignored directory for verified reference TSV files",
    )
    parser.add_argument(
        "--selection",
        type=Path,
        default=DEFAULT_SELECTION,
        help="frozen public episode/sample selection",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        actual = reproduce(
            args.source_dir.resolve(),
            args.output_dir.resolve(),
            args.selection.resolve(),
        )
    except ReproductionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for regime in EXPECTED_SHA256:
        print(f"{regime}: {actual[regime]}")
    print(f"verified references written to {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
