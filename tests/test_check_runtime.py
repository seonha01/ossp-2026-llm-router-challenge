# SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import importlib.util
import io
import pathlib
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from ossp_router.runtime import AttemptKind, AttemptResult


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "check_runtime",
    ROOT / "tools/check_runtime.py",
)
assert SPEC is not None and SPEC.loader is not None
check_runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check_runtime)


class ParticipantRuntimeCheckTest(unittest.TestCase):
    def test_container_command_uses_official_resource_flags(self) -> None:
        image_id = "sha256:" + "a" * 64
        with tempfile.TemporaryDirectory() as temporary:
            target = pathlib.Path(temporary)
            input_path = target / "inputs.json"
            output_directory = target / "output"
            cidfile = target / "container.cid"
            input_path.write_text("{}", encoding="utf-8")
            output_directory.mkdir()
            command = check_runtime._container_arguments(
                docker="/usr/bin/docker",
                image_id=image_id,
                input_path=input_path,
                output_directory=output_directory,
                tier="fast",
                cidfile=cidfile,
                container_name="ossp-router-check-test",
            )

        self.assertEqual("/usr/bin/docker", command[0])
        self.assertIn(image_id, command)
        self.assertIn("--pull", command)
        self.assertEqual("never", command[command.index("--pull") + 1])
        self.assertEqual("linux/arm64", command[command.index("--platform") + 1])
        self.assertEqual("none", command[command.index("--network") + 1])
        self.assertEqual("2", command[command.index("--cpus") + 1])
        self.assertEqual("2g", command[command.index("--memory") + 1])
        self.assertEqual("2g", command[command.index("--memory-swap") + 1])
        self.assertEqual("32", command[command.index("--pids-limit") + 1])
        self.assertIn("--read-only", command)
        self.assertIn("--cap-drop", command)
        self.assertIn("no-new-privileges", command)
        self.assertIn("/challenge/input/inputs.json", command)
        self.assertIn("/challenge/output/submission.json", command)

    def test_image_tag_is_resolved_to_local_arm64_id(self) -> None:
        image_id = "sha256:" + "b" * 64
        metadata = {
            "Id": image_id,
            "Os": "linux",
            "Architecture": "arm64",
            "Config": {"Volumes": None},
        }
        with mock.patch.object(
            check_runtime,
            "inspect_image_runtime_metadata",
            return_value=metadata,
        ):
            self.assertEqual(
                image_id,
                check_runtime._resolve_image("/usr/bin/docker", "router:local"),
            )

    def test_valid_output_directory_must_have_one_file(self) -> None:
        result = AttemptResult(
            AttemptKind.VALID,
            "valid",
            measurement_elapsed_ns=1_000_000_000,
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = pathlib.Path(temporary)
            (output / "submission.json").write_text("{}", encoding="utf-8")
            checked = check_runtime._validate_output_directory(result, output)
            self.assertIs(result, checked)
            (output / "extra.txt").write_text("x", encoding="utf-8")
            checked = check_runtime._validate_output_directory(result, output)
        self.assertEqual(AttemptKind.EXECUTION_FAILURE, checked.kind)

    def test_result_record_reports_elapsed_seconds(self) -> None:
        record = check_runtime._result_record(
            AttemptResult(
                AttemptKind.VALID,
                "valid",
                returncode=0,
                measurement_elapsed_ns=1_250_000_000,
            ),
            1,
        )
        self.assertTrue(record["passed"])
        self.assertEqual(1.25, record["elapsed_seconds"])
        self.assertEqual(90, record["limit_seconds"])

    def test_main_returns_failure_for_failed_report(self) -> None:
        report = {
            "workload": {
                "episodes": 2_640,
                "bytes": 10,
                "sha256": "a" * 64,
            },
            "tiers": [
                {
                    "tier": "fast",
                    "passed": False,
                    "elapsed_seconds_max": 91.0,
                    "repetitions": [
                        {
                            "repetition": 1,
                            "passed": False,
                            "detail": "timeout",
                        }
                    ],
                }
            ],
            "limits": {"wall_time_seconds_per_tier": 90},
            "passed": False,
            "warnings": [],
        }
        with mock.patch.object(
            check_runtime,
            "_resolve_docker",
            return_value="/usr/bin/docker",
        ), mock.patch.object(
            check_runtime,
            "check_image",
            return_value=report,
        ):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(
                    1,
                    check_runtime.main(["--image", "router:local"]),
                )


if __name__ == "__main__":
    unittest.main()
