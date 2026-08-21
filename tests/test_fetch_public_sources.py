# SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
# SPDX-License-Identifier: Apache-2.0

import hashlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "fetch_public_sources", ROOT / "tools" / "fetch_public_sources.py"
)
assert SPEC is not None and SPEC.loader is not None
FETCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FETCH)


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()


class FetchPublicSourcesTests(unittest.TestCase):
    def test_repository_source_pins_are_valid_and_unique(self):
        sources = FETCH.load_source_pins(FETCH.DEFAULT_PINS)
        self.assertIn("aime24-public", sources)
        self.assertIn("aime25-public", sources)
        self.assertEqual(
            {"aime24-public", "aime25-public"},
            {source_id for source_id in sources if source_id.startswith("aime")},
        )
        for source in sources.values():
            for file_record in source.get("files", []):
                FETCH._safe_path(file_record["path"], "path")
                FETCH._sha256_text(file_record["sha256"], "sha256")
                FETCH.source_file_url(source, file_record)

    def test_babilong_pins_cover_4k_and_16k_and_math_selection_is_bound(self):
        sources = FETCH.load_source_pins(FETCH.DEFAULT_PINS)
        babilong = sources["babilong-4k-16k"]
        self.assertEqual(
            {
                f"data/qa{task}/{context}.json"
                for task in range(1, 11)
                for context in ("4k", "16k")
            },
            {record["path"] for record in babilong["files"]},
        )
        mathematics = sources["deepmind-mathematics"]
        selection = ROOT / "data/sources" / mathematics["public_selection"]
        self.assertEqual(
            mathematics["public_selection_sha256"],
            hashlib.sha256(selection.read_bytes()).hexdigest(),
        )

    def test_huggingface_url_uses_pinned_revision(self):
        source = {
            "source_type": "huggingface",
            "repo_id": "owner/data",
            "revision": "a" * 40,
        }
        url = FETCH.source_file_url(source, {"path": "data/test file.json"})
        self.assertEqual(
            url,
            "https://huggingface.co/datasets/owner/data/resolve/"
            + "a" * 40
            + "/data/test%20file.json",
        )

    def test_unsafe_paths_are_rejected(self):
        for value in ("../secret", "/absolute", "safe/../secret", "."):
            with self.subTest(value=value), self.assertRaises(FETCH.SourceFetchError):
                FETCH._safe_path(value, "path")

    def test_download_is_verified_and_cached(self):
        content = b"public source\n"
        expected = hashlib.sha256(content).hexdigest()
        source = {
            "source_id": "example",
            "source_type": "remote-file",
            "revision": "b" * 40,
            "files": [
                {
                    "path": "nested/data.json",
                    "sha256": expected,
                    "url": "https://example.invalid/data.json",
                }
            ],
        }
        calls = []

        def opener(url, timeout):
            calls.append((url, timeout))
            return _Response(content)

        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary)
            first = FETCH.fetch_source(source, cache, opener=opener)
            second = FETCH.fetch_source(source, cache, opener=opener)
            destination = cache / "example" / ("b" * 40) / "nested/data.json"
            self.assertEqual(content, destination.read_bytes())
            self.assertEqual("downloaded", first[0][1])
            self.assertEqual("cached", second[0][1])
            self.assertEqual(1, len(calls))

    def test_hash_mismatch_leaves_no_destination(self):
        source = {
            "source_id": "example",
            "source_type": "remote-file",
            "revision": "c" * 40,
            "files": [
                {
                    "path": "data.json",
                    "sha256": "0" * 64,
                    "url": "https://example.invalid/data.json",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary)
            with self.assertRaises(FETCH.SourceFetchError):
                FETCH.fetch_source(
                    source,
                    cache,
                    opener=lambda url, timeout: _Response(b"wrong"),
                )
            destination = cache / "example" / ("c" * 40) / "data.json"
            self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
