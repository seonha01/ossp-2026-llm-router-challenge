# SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
# SPDX-License-Identifier: Apache-2.0

import hashlib
import importlib.util
import os
import random
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "reproduce_deepmind_mathematics",
    ROOT / "tools" / "reproduce_deepmind_mathematics.py",
)
assert SPEC is not None and SPEC.loader is not None
RECIPE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RECIPE)


class _FakeNumpyRandom:
    def __init__(self):
        self.state = ("numpy", 7)

    def get_state(self):
        return self.state

    def set_state(self, state):
        self.state = state

    def seed(self, seed):
        self.state = ("seeded", seed)


class _FakeNumpy:
    def __init__(self):
        self.random = _FakeNumpyRandom()


class _FakeSympyRng:
    def __init__(self):
        self.state = ("sympy", 11)

    def getstate(self):
        return self.state

    def setstate(self, state):
        self.state = state

    def seed(self, seed):
        self.state = ("seeded", seed)


class ReproduceDeepMindMathematicsTests(unittest.TestCase):
    def test_generation_seed_uses_nul_delimited_big_endian_sha256_prefix(self):
        fields = (
            RECIPE.UPSTREAM_REVISION,
            str(RECIPE.ROOT_SEED),
            "interpolate",
            "algebra__linear_1d",
            "3",
            "2",
        )
        expected = int.from_bytes(
            hashlib.sha256("\0".join(fields).encode("utf-8")).digest()[:4],
            "big",
        )
        self.assertEqual(
            expected,
            RECIPE.generation_seed("interpolate", "algebra__linear_1d", 3, 2),
        )

    def test_flatten_modules_sorts_category_and_module_names(self):
        def one():
            return None

        def two():
            return None

        flattened = RECIPE.flatten_modules(
            {"zeta": {"second": two}, "alpha": {"first": one}}
        )
        self.assertEqual(["alpha__first", "zeta__second"], list(flattened))

    def test_rng_states_are_restored_after_success_and_error(self):
        numpy = _FakeNumpy()
        sympy_rng = _FakeSympyRng()
        python_state = random.getstate()
        numpy_state = numpy.random.get_state()
        sympy_state = sympy_rng.getstate()
        with RECIPE.isolated_rng_state(1234, numpy, sympy_rng):
            self.assertEqual(("seeded", 1234), numpy.random.get_state())
            self.assertEqual(("seeded", 1234), sympy_rng.getstate())
        self.assertEqual(python_state, random.getstate())
        self.assertEqual(numpy_state, numpy.random.get_state())
        self.assertEqual(sympy_state, sympy_rng.getstate())

        with self.assertRaisesRegex(RuntimeError, "boom"):
            with RECIPE.isolated_rng_state(5678, numpy, sympy_rng):
                raise RuntimeError("boom")
        self.assertEqual(python_state, random.getstate())
        self.assertEqual(numpy_state, numpy.random.get_state())
        self.assertEqual(sympy_state, sympy_rng.getstate())

    def test_reference_serialization_has_tabs_lf_and_no_final_lf(self):
        samples = [
            RECIPE.Sample("id-1", "one", "1"),
            RECIPE.Sample("id-2", "two", "2"),
        ]
        self.assertEqual(b"id-1\tone\t1\nid-2\ttwo\t2", RECIPE.reference_bytes(samples))

    def test_hash_mismatch_is_fail_closed(self):
        bad = {
            regime: [RECIPE.Sample(f"{regime}-{index}", "q", "a")
                     for index in range(RECIPE.SAMPLES_PER_REGIME)]
            for regime in RECIPE.EXPECTED_SHA256
        }
        with self.assertRaisesRegex(RECIPE.ReproductionError, "SHA-256 mismatch"):
            RECIPE.verify_reference(bad)

    def test_repository_public_selection_is_sanitized_and_frozen(self):
        selection = RECIPE.load_public_selection(RECIPE.DEFAULT_SELECTION)
        self.assertEqual(303, len(selection["train"]))
        self.assertEqual(153, len(selection["dev"]))
        for rows in selection.values():
            for row in rows:
                self.assertEqual({"episode_id", "sample_id"}, set(row))

    @unittest.skipUnless(
        os.environ.get("OSSP_DEEPMIND_MATHEMATICS_SOURCE_DIR"),
        "set OSSP_DEEPMIND_MATHEMATICS_SOURCE_DIR for the full 1,800-row check",
    )
    def test_full_reference_hashes(self):
        source_dir = Path(os.environ["OSSP_DEEPMIND_MATHEMATICS_SOURCE_DIR"])
        with tempfile.TemporaryDirectory() as temporary:
            actual = RECIPE.reproduce(source_dir, Path(temporary))
        self.assertEqual(RECIPE.EXPECTED_SHA256, actual)


if __name__ == "__main__":
    unittest.main()
