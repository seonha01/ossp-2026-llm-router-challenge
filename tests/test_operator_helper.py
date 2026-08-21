# SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import contextlib
import io
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from ossp_router.operator_helper import (
    PARTICIPANT_OUTPUT_ERROR,
    _extract,
)


class OperatorOutputHelperTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.target = pathlib.Path(self.temporary.name)
        self.source = self.target / "source"
        self.source.mkdir()
        self.destination = self.target / "result" / "submission.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _extract(self, maximum: int) -> int:
        with contextlib.redirect_stderr(io.StringIO()):
            return _extract(self.source, self.destination, maximum)

    def test_extracts_exactly_one_regular_file(self) -> None:
        payload = b'{"ok":true}\n'
        (self.source / "submission.json").write_bytes(payload)
        self.assertEqual(0, self._extract(len(payload)))
        self.assertEqual(payload, self.destination.read_bytes())
        self.assertEqual(0o644, self.destination.stat().st_mode & 0o777)

    def test_rejects_small_auxiliary_file(self) -> None:
        (self.source / "submission.json").write_text("{}", encoding="utf-8")
        (self.source / ".hidden").write_text("x", encoding="utf-8")
        self.assertEqual(
            PARTICIPANT_OUTPUT_ERROR,
            self._extract(1024),
        )
        self.assertFalse(self.destination.exists())

    def test_rejects_auxiliary_directory(self) -> None:
        (self.source / "submission.json").write_text("{}", encoding="utf-8")
        (self.source / "nested").mkdir()
        self.assertEqual(PARTICIPANT_OUTPUT_ERROR, self._extract(1024))
        self.assertFalse(self.destination.exists())

    def test_rejects_sparse_file_without_copying_it(self) -> None:
        output = self.source / "submission.json"
        with output.open("wb") as stream:
            stream.seek(4 * 1024 * 1024 * 1024 - 1)
            stream.write(b"x")
        self.assertEqual(
            PARTICIPANT_OUTPUT_ERROR,
            self._extract(4 * 1024 * 1024),
        )
        self.assertFalse(self.destination.exists())

    def test_rejects_symbolic_link(self) -> None:
        outside = self.target / "outside.json"
        outside.write_text("{}", encoding="utf-8")
        (self.source / "submission.json").symlink_to(outside)
        self.assertEqual(
            PARTICIPANT_OUTPUT_ERROR,
            self._extract(1024),
        )
        self.assertFalse(self.destination.exists())

    def test_rejects_fifo_without_opening_it(self) -> None:
        output = self.source / "submission.json"
        os.mkfifo(output)
        self.assertEqual(
            PARTICIPANT_OUTPUT_ERROR,
            self._extract(1024),
        )
        self.assertFalse(self.destination.exists())

    def test_rejects_hard_linked_submission(self) -> None:
        output = self.source / "submission.json"
        output.write_text("{}", encoding="utf-8")
        os.link(output, self.target / "outside-hard-link")
        self.assertEqual(
            PARTICIPANT_OUTPUT_ERROR,
            self._extract(1024),
        )
        self.assertFalse(self.destination.exists())

    def test_permission_denied_source_is_participant_output_error(self) -> None:
        (self.source / "submission.json").write_text("{}", encoding="utf-8")
        with mock.patch(
            "ossp_router.operator_helper.os.scandir",
            side_effect=PermissionError("participant changed directory mode"),
        ):
            self.assertEqual(PARTICIPANT_OUTPUT_ERROR, self._extract(1024))
        self.assertFalse(self.destination.exists())

    def test_rejects_submission_changed_while_reading(self) -> None:
        output = self.source / "submission.json"
        output.write_text('{"ok":true}', encoding="utf-8")
        metadata = output.stat()
        changed = mock.Mock(
            st_ino=metadata.st_ino,
            st_size=metadata.st_size,
            st_mtime_ns=metadata.st_mtime_ns + 1,
        )
        stderr = io.StringIO()
        with mock.patch(
            "ossp_router.operator_helper.os.fstat",
            side_effect=(metadata, changed),
        ), contextlib.redirect_stderr(stderr):
            result = _extract(self.source, self.destination, 1024)
        self.assertEqual(PARTICIPANT_OUTPUT_ERROR, result)
        self.assertIn("검사 중 변경", stderr.getvalue())
        self.assertFalse(self.destination.exists())


if __name__ == "__main__":
    unittest.main()
