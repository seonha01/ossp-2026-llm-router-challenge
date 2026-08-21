# SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
import venv
import zipfile


ROOT = pathlib.Path(__file__).resolve().parents[1]


class CliTest(unittest.TestCase):
    def test_validate_technical_submission_accepts_exact_public_fields(self) -> None:
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(ROOT / "src")
        with tempfile.TemporaryDirectory() as temporary:
            submission = pathlib.Path(temporary) / "submission-ossp-skt.json"
            submission.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "challenge_id": "ossp-2026-llm-router-challenge",
                        "repository_url": "https://github.com/example/router",
                        "commit_sha": "0" * 40,
                        "image_digest": (
                            "ghcr.io/example/router@sha256:" + "1" * 64
                        ),
                        "primary_license": "Apache-2.0",
                    }
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "tools/validate_technical_submission.py"),
                    "--file",
                    str(submission),
                ],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("기술 제출 정보가 유효합니다", result.stdout)

    def test_validate_technical_submission_rejects_extra_field(self) -> None:
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(ROOT / "src")
        with tempfile.TemporaryDirectory() as temporary:
            submission = pathlib.Path(temporary) / "submission-ossp-skt.json"
            submission.write_text(
                """{
  "schema_version": 1,
  "challenge_id": "ossp-2026-llm-router-challenge",
  "repository_url": "https://github.com/example/router",
  "commit_sha": "0123456789abcdef0123456789abcdef01234567",
  "image_digest": "ghcr.io/example/router@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "primary_license": "Apache-2.0",
  "note": "not allowed"
}
""",
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "tools/validate_technical_submission.py"),
                    "--file",
                    str(submission),
                ],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(2, result.returncode)
            self.assertIn("허용되지 않은 필드", result.stderr)

    def test_baseline_and_self_check_are_reproducible_across_processes(self) -> None:
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(ROOT / "src")
        with tempfile.TemporaryDirectory() as temporary:
            target = pathlib.Path(temporary)
            submissions = target / "submissions"
            baseline = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "baselines/always_light.py"),
                    "--input",
                    str(ROOT / "data/toy/inputs.json"),
                    "--output-dir",
                    str(submissions),
                ],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, baseline.returncode, baseline.stderr)
            report_bytes = []
            for index in range(2):
                report = target / f"report-{index}.json"
                checked = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "ossp_router.cli",
                        "self-check",
                        "--input",
                        str(ROOT / "data/toy/inputs.json"),
                        "--outcomes",
                        str(ROOT / "data/toy/outcomes.json"),
                        "--submissions",
                        str(submissions),
                        "--report",
                        str(report),
                    ],
                    cwd=ROOT,
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(0, checked.returncode, checked.stderr)
                report_bytes.append(report.read_bytes())
            self.assertEqual(report_bytes[0], report_bytes[1])
            self.assertEqual("0.5", json.loads(report_bytes[0])["final_score"])

    def test_wheel_console_script_uses_bundled_policy_outside_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = pathlib.Path(temporary)
            source = target / "source"
            shutil.copytree(
                ROOT,
                source,
                ignore=shutil.ignore_patterns(
                    ".git",
                    ".venv",
                    "*.egg-info",
                    "__pycache__",
                    "build",
                    "dist",
                ),
            )
            wheelhouse = target / "wheelhouse"
            built = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "wheel",
                    "--no-build-isolation",
                    "--no-deps",
                    "--wheel-dir",
                    str(wheelhouse),
                    str(source),
                ],
                cwd=target,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, built.returncode, built.stderr)
            wheel = next(wheelhouse.glob("*.whl"))
            with zipfile.ZipFile(wheel) as archive:
                names = archive.namelist()
            self.assertTrue(
                any(
                    name.endswith("/NOTICE")
                    or name.endswith("/licenses/NOTICE")
                    for name in names
                )
            )
            self.assertTrue(
                any(
                    name.endswith("/Apache-2.0.txt")
                    or
                    name.endswith("/licenses/LICENSES/Apache-2.0.txt")
                    for name in names
                )
            )

            environment_dir = target / "venv"
            venv.EnvBuilder(with_pip=True).create(environment_dir)
            environment_python = environment_dir / "bin" / "python"
            installed = subprocess.run(
                [
                    str(environment_python),
                    "-m",
                    "pip",
                    "install",
                    "--no-deps",
                    str(wheel),
                ],
                cwd=target,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, installed.returncode, installed.stderr)

            environment = dict(os.environ)
            environment.pop("PYTHONPATH", None)
            submissions = target / "installed-submissions"
            console = environment_dir / "bin" / "ossp-router"
            generated = subprocess.run(
                [
                    str(console),
                    "always-light",
                    "--input",
                    str(ROOT / "data/toy/inputs.json"),
                    "--output-dir",
                    str(submissions),
                ],
                cwd=target,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, generated.returncode, generated.stderr)
            report = target / "installed-report.json"
            checked = subprocess.run(
                [
                    str(console),
                    "self-check",
                    "--input",
                    str(ROOT / "data/toy/inputs.json"),
                    "--outcomes",
                    str(ROOT / "data/toy/outcomes.json"),
                    "--submissions",
                    str(submissions),
                    "--report",
                    str(report),
                ],
                cwd=target,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, checked.returncode, checked.stderr)
            self.assertEqual("0.5", json.loads(report.read_bytes())["final_score"])

            one_tier = target / "installed-balanced.json"
            router_run = environment_dir / "bin" / "router-run"
            generated_tier = subprocess.run(
                [
                    str(router_run),
                    "--input",
                    str(ROOT / "data/toy/inputs.json"),
                    "--tier",
                    "balanced",
                    "--output",
                    str(one_tier),
                ],
                cwd=target,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, generated_tier.returncode, generated_tier.stderr)
            self.assertEqual("balanced", json.loads(one_tier.read_bytes())["tier"])

            evaluator = environment_dir / "bin" / "router-evaluate-tier"
            evaluator_help = subprocess.run(
                [str(evaluator), "--help"],
                cwd=target,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, evaluator_help.returncode, evaluator_help.stderr)
            self.assertIn("--commit-sha", evaluator_help.stdout)
            cleanup = environment_dir / "bin" / "router-cleanup-resources"
            cleanup_help = subprocess.run(
                [str(cleanup), "--help"],
                cwd=target,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, cleanup_help.returncode, cleanup_help.stderr)
            self.assertIn(
                "--confirm-daemon-restarted",
                cleanup_help.stdout,
            )
            image_measure = environment_dir / "bin" / "router-measure-image"
            image_measure_help = subprocess.run(
                [str(image_measure), "--help"],
                cwd=target,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                0,
                image_measure_help.returncode,
                image_measure_help.stderr,
            )
            self.assertIn("--oci-layout", image_measure_help.stdout)
            self.assertIn("--cleanup-pending", image_measure_help.stdout)
            helper_check = subprocess.run(
                [
                    str(environment_python),
                    "-c",
                    (
                        "from ossp_router.runtime import OPERATOR_HELPER_PATH;"
                        "assert OPERATOR_HELPER_PATH.is_file()"
                    ),
                ],
                cwd=target,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, helper_check.returncode, helper_check.stderr)

    def test_invalid_input_returns_nonzero(self) -> None:
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(ROOT / "src")
        with tempfile.TemporaryDirectory() as temporary:
            bad_input = pathlib.Path(temporary) / "bad.json"
            bad_input.write_text('{"episodes":[]}', encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "ossp_router.cli",
                    "validate-input",
                    "--input",
                    str(bad_input),
                ],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(2, result.returncode)
            self.assertIn("오류:", result.stderr)


if __name__ == "__main__":
    unittest.main()
