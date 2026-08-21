# SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import importlib.util
import json
import pathlib
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "benchmark_runtime",
    ROOT / "tools/benchmark_runtime.py",
)
assert SPEC is not None and SPEC.loader is not None
benchmark_runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark_runtime)


class AppleSiliconEnvironmentProbeTest(unittest.TestCase):
    def _probe(self, **overrides):
        arguments = {
            "docker": "/usr/bin/docker",
            "colima": "/opt/homebrew/bin/colima",
            "colima_profile": "default",
            "attestation": (
                benchmark_runtime.APPLE_SILICON_OPERATOR_ATTESTATION
            ),
            "environment_label": "apple-m3-pro-colima",
        }
        arguments.update(overrides)
        return benchmark_runtime._probe_apple_silicon_environment(**arguments)

    def test_official_mode_requires_explicit_attestation(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "attestation"):
            self._probe(attestation=None)

    def test_official_mode_requires_environment_label(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "environment-label"):
            self._probe(environment_label=None)

    def test_official_mode_rejects_non_apple_silicon_host(self) -> None:
        with mock.patch.object(
            benchmark_runtime.platform,
            "system",
            return_value="Linux",
        ), mock.patch.object(
            benchmark_runtime.platform,
            "machine",
            return_value="x86_64",
        ):
            with self.assertRaisesRegex(RuntimeError, "Apple Silicon"):
                self._probe()

    def test_official_mode_rejects_missing_docker(self) -> None:
        with mock.patch.object(
            benchmark_runtime.platform,
            "system",
            return_value="Darwin",
        ), mock.patch.object(
            benchmark_runtime.platform,
            "machine",
            return_value="arm64",
        ):
            with self.assertRaisesRegex(RuntimeError, "Docker CLI"):
                self._probe(docker=None)

    def test_official_probe_records_colima_and_docker_evidence(self) -> None:
        docker_info = {
            "OSType": "linux",
            "Architecture": "aarch64",
            "CgroupVersion": "2",
            "CgroupDriver": "systemd",
            "Driver": "overlay2",
            "ServerVersion": "27.0.0",
            "Name": "colima",
        }
        colima_status = {
            "status": "Running",
            "arch": "aarch64",
            "cpus": 4,
            "memory": 6 * 1024**3,
            "disk": 20 * 1024**3,
            "runtime": "docker",
        }
        with mock.patch.object(
            benchmark_runtime.platform,
            "system",
            return_value="Darwin",
        ), mock.patch.object(
            benchmark_runtime.platform,
            "machine",
            return_value="arm64",
        ), mock.patch.object(
            benchmark_runtime.subprocess,
            "run",
            return_value=mock.Mock(
                returncode=0,
                stdout=json.dumps(colima_status),
                stderr="",
            ),
        ), mock.patch.object(
            benchmark_runtime,
            "_docker_json",
            return_value=docker_info,
        ), mock.patch.object(
            benchmark_runtime,
            "_local_docker_context_evidence",
            return_value={
                "context": "default",
                "endpoint_type": "local-unix-socket",
                "daemon_name": "colima",
                "virtualization_boundary": "local-colima-linux-vm",
            },
        ):
            evidence = self._probe()
        self.assertEqual("Darwin", evidence["host"]["system"])
        self.assertEqual("aarch64", evidence["colima"]["architecture"])
        self.assertEqual(4, evidence["colima"]["cpus"])
        self.assertEqual("2", evidence["docker"]["cgroup_version"])
        self.assertEqual("overlay2", evidence["docker"]["storage_driver"])
        self.assertEqual(
            "apple-m3-pro-colima",
            evidence["environment_label"],
        )
        self.assertEqual(
            "local-unix-socket",
            evidence["docker"]["local_context"]["endpoint_type"],
        )

    def test_official_probe_rejects_x86_colima_profile(self) -> None:
        with mock.patch.object(
            benchmark_runtime.platform,
            "system",
            return_value="Darwin",
        ), mock.patch.object(
            benchmark_runtime.platform,
            "machine",
            return_value="arm64",
        ), mock.patch.object(
            benchmark_runtime.subprocess,
            "run",
            return_value=mock.Mock(
                returncode=0,
                stdout=json.dumps({"arch": "x86_64", "runtime": "docker"}),
                stderr="",
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "aarch64"):
                self._probe()

    def test_official_main_fails_before_writing_without_image(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = pathlib.Path(temporary)
            json_output = target / "report.json"
            markdown_output = target / "report.md"
            with self.assertRaises(SystemExit):
                benchmark_runtime.main(
                    [
                        "--measurement-mode",
                        "apple-silicon-colima",
                        "--apple-silicon-operator-attestation",
                        benchmark_runtime.APPLE_SILICON_OPERATOR_ATTESTATION,
                        "--json-output",
                        str(json_output),
                        "--markdown-output",
                        str(markdown_output),
                    ]
                )
            self.assertFalse(json_output.exists())
            self.assertFalse(markdown_output.exists())

    def test_parser_defaults_to_materialized_public_inputs(self) -> None:
        arguments = benchmark_runtime._parser().parse_args([])
        self.assertEqual(
            benchmark_runtime.DEFAULT_TRAIN_INPUT,
            arguments.train_input,
        )
        self.assertEqual(
            benchmark_runtime.DEFAULT_DEV_INPUT,
            arguments.dev_input,
        )

    def test_official_context_rejects_remote_endpoint(self) -> None:
        completed = mock.Mock(
            returncode=0,
            stdout="default\n",
            stderr="",
        )
        inspected = mock.Mock(
            returncode=0,
            stdout='"tcp://remote.example:2376"\n',
            stderr="",
        )
        with mock.patch.dict(benchmark_runtime.os.environ, {}, clear=True):
            with mock.patch.object(
                benchmark_runtime.subprocess,
                "run",
                side_effect=(completed, inspected),
            ):
                with self.assertRaisesRegex(RuntimeError, "원격 daemon"):
                    benchmark_runtime._local_docker_context_evidence(
                        "/usr/bin/docker",
                        {"Name": "colima"},
                    )

    def test_official_context_records_colima_vm_boundary(self) -> None:
        completed = mock.Mock(
            returncode=0,
            stdout="default\n",
            stderr="",
        )
        inspected = mock.Mock(
            returncode=0,
            stdout='"unix:///var/run/docker.sock"\n',
            stderr="",
        )
        with mock.patch.dict(benchmark_runtime.os.environ, {}, clear=True):
            with mock.patch.object(
                benchmark_runtime.subprocess,
                "run",
                side_effect=(completed, inspected),
            ):
                evidence = benchmark_runtime._local_docker_context_evidence(
                    "/usr/bin/docker",
                    {"Name": "colima"},
                )
        self.assertEqual("colima", evidence["daemon_name"])
        self.assertEqual(
            "local-colima-linux-vm",
            evidence["virtualization_boundary"],
        )


class BenchmarkEvidenceTest(unittest.TestCase):
    def test_image_reference_is_resolved_to_local_content_id(self) -> None:
        image_id = "sha256:" + "a" * 64
        completed = mock.Mock(
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "Id": image_id,
                        "Architecture": "arm64",
                        "Os": "linux",
                        "RepoDigests": ["router@sha256:" + "b" * 64],
                        "Size": 123,
                    }
                ]
            ),
            stderr="",
        )
        with mock.patch.object(
            benchmark_runtime.subprocess,
            "run",
            return_value=completed,
        ) as run:
            resolved = benchmark_runtime._resolve_container_image(
                "/usr/bin/docker",
                "router:latest",
            )
        self.assertEqual(image_id, resolved["resolved_local_content_id"])
        self.assertEqual(image_id, resolved["config_digest"])
        self.assertEqual(
            ["/usr/bin/docker", "image", "inspect", "router:latest"],
            run.call_args.args[0],
        )

    def test_source_manifest_and_artifact_hashes_are_complete(self) -> None:
        manifest = benchmark_runtime._source_tree_manifest()
        artifacts = benchmark_runtime._artifact_hashes()
        self.assertEqual(64, len(manifest["sha256"]))
        paths = {entry["path"] for entry in manifest["entries"]}
        self.assertIn("container/Dockerfile", paths)
        self.assertIn("container/measurement.Dockerfile", paths)
        self.assertIn("container/entrypoint.py", paths)
        self.assertIn("src/ossp_router/runtime.py", paths)
        self.assertIn("baselines/feature_budget.py", paths)
        self.assertIn("baselines/hash_regex.py", paths)
        self.assertIn("baselines/hash-regex-public.v1.json", paths)
        self.assertNotIn("tools/benchmark_runtime.py", paths)
        self.assertFalse(any(".egg-info/" in path for path in paths))
        self.assertEqual(
            {
                "benchmark_tool_sha256",
                "dockerfile_sha256",
                "measurement_dockerfile_sha256",
                "policy_sha256",
                "representative_router_artifact_sha256",
            },
            set(artifacts),
        )
        self.assertTrue(all(len(value) == 64 for value in artifacts.values()))

    def test_stale_or_unlabeled_reference_image_is_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "label"):
            benchmark_runtime._require_image_source_binding(
                {"source_manifest_sha256": None},
                current_source_manifest_sha256="a" * 64,
            )
        with self.assertRaisesRegex(RuntimeError, "stale"):
            benchmark_runtime._require_image_source_binding(
                {"source_manifest_sha256": "b" * 64},
                current_source_manifest_sha256="a" * 64,
            )
        benchmark_runtime._require_image_source_binding(
            {"source_manifest_sha256": "a" * 64},
            current_source_manifest_sha256="a" * 64,
        )

    def test_cgroup_parser_accepts_required_v2_metrics(self) -> None:
        metrics = {
            "cgroup_v2": {
                "cpu_stat": {
                    "usage_usec": 100,
                    "user_usec": 80,
                    "system_usec": 20,
                    "nr_periods": 10,
                    "nr_throttled": 1,
                    "throttled_usec": 5,
                },
                "cpu_usage_delta_usec": 50,
                "memory_peak_bytes": 4096,
                "memory_events": {"max": 0, "oom": 0, "oom_kill": 0},
                "pids_peak": 2,
                "pids_current": 1,
                "pids_events": {"max": 0},
            }
        }
        parsed = benchmark_runtime._validate_cgroup_v2_metrics(metrics)
        self.assertEqual(4096, parsed["memory_peak_bytes"])
        self.assertEqual(2, parsed["pids_peak"])

    def test_cgroup_parser_fails_closed_when_peak_is_missing(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "정수 측정값"):
            benchmark_runtime._validate_cgroup_v2_metrics(
                {
                    "cgroup_v2": {
                        "cpu_stat": {
                            "usage_usec": 100,
                            "user_usec": 80,
                            "system_usec": 20,
                            "nr_periods": 10,
                            "nr_throttled": 1,
                            "throttled_usec": 5,
                        },
                        "cpu_usage_delta_usec": 50,
                        "memory_events": {
                            "max": 0,
                            "oom": 0,
                            "oom_kill": 0,
                        },
                        "pids_peak": 2,
                        "pids_current": 1,
                        "pids_events": {"max": 0},
                    }
                }
            )

    def test_cgroup_parser_requires_throttling_fields(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "cpu.stat"):
            benchmark_runtime._validate_cgroup_v2_metrics(
                {
                    "cgroup_v2": {
                        "cpu_stat": {
                            "usage_usec": 100,
                            "user_usec": 80,
                            "system_usec": 20,
                        },
                        "cpu_usage_delta_usec": 50,
                        "memory_peak_bytes": 4096,
                        "memory_events": {
                            "max": 0,
                            "oom": 0,
                            "oom_kill": 0,
                        },
                        "pids_peak": 2,
                        "pids_current": 1,
                        "pids_events": {"max": 0},
                    }
                }
            )

    def test_cgroup_parser_rejects_limit_events_for_native_completion(
        self,
    ) -> None:
        with self.assertRaisesRegex(RuntimeError, "후보 한도"):
            benchmark_runtime._validate_cgroup_v2_metrics(
                {
                    "cgroup_v2": {
                        "cpu_stat": {
                            "usage_usec": 100,
                            "user_usec": 80,
                            "system_usec": 20,
                            "nr_periods": 10,
                            "nr_throttled": 0,
                            "throttled_usec": 0,
                        },
                        "cpu_usage_delta_usec": 50,
                        "memory_peak_bytes": 4096,
                        "memory_events": {
                            "max": 1,
                            "oom": 0,
                            "oom_kill": 0,
                        },
                        "pids_peak": 2,
                        "pids_current": 1,
                        "pids_events": {"max": 0},
                    }
                }
            )

    def test_container_run_uses_resolved_id_and_official_isolation_flags(
        self,
    ) -> None:
        image_id = "sha256:" + "c" * 64
        cgroup = {
            "cpu_stat": {
                "usage_usec": 100,
                "user_usec": 80,
                "system_usec": 20,
                "nr_periods": 10,
                "nr_throttled": 1,
                "throttled_usec": 5,
            },
            "cpu_usage_delta_usec": 50,
            "memory_peak_bytes": 4096,
            "memory_events": {"max": 0, "oom": 0, "oom_kill": 0},
            "pids_peak": 2,
            "pids_current": 1,
            "pids_events": {"max": 0},
        }
        worker_metrics = {
            "cpu_seconds": 0.1,
            "max_rss_bytes": 4096,
            "processes": 1,
            "threads": 1,
            "cgroup_v2": cgroup,
        }
        with tempfile.TemporaryDirectory() as temporary:
            target = pathlib.Path(temporary)
            input_path = target / "inputs.json"
            output_path = target / "output/submission.json"
            benchmark_runtime.write_json(
                input_path,
                {
                    "schema_version": 1,
                    "challenge_id": "runtime-test",
                    "split": "public-train-dev",
                    "episodes": [
                        {"episode_id": "public-1", "prompt": "public prompt"}
                    ],
                },
            )
            inputs = benchmark_runtime.load_input(input_path)
            submission = benchmark_runtime.make_submission(
                inputs,
                benchmark_runtime.load_bundled_policy(),
                "fast",
                strategy="always-light",
            )

            def completed(command, **_kwargs):
                benchmark_runtime.write_submission_atomic(
                    output_path,
                    submission,
                )
                return mock.Mock(
                    returncode=0,
                    stdout=json.dumps(worker_metrics),
                    stderr="",
                )

            with mock.patch.object(
                benchmark_runtime.subprocess,
                "run",
                side_effect=completed,
            ) as run:
                result = benchmark_runtime._run_container_once(
                    docker="/usr/bin/docker",
                    image=image_id,
                    strategy="always-light",
                    tier="fast",
                    input_path=input_path,
                    output_path=output_path,
                    inputs=inputs,
                    require_official_cgroup=True,
                )
        command = run.call_args.args[0]
        self.assertIn(image_id, command)
        pull_index = command.index("--pull")
        self.assertEqual("never", command[pull_index + 1])
        cgroupns_index = command.index("--cgroupns")
        self.assertEqual("private", command[cgroupns_index + 1])
        self.assertIn("--ipc", command)
        self.assertIn("--log-driver", command)
        self.assertIn("--ulimit", command)
        ulimit_index = command.index("--ulimit")
        self.assertEqual("core=0:0", command[ulimit_index + 1])
        self.assertEqual(cgroup, result["cgroup_v2"])
        public_command = result["container_argv"]
        self.assertEqual("<docker-cli>", public_command[0])
        self.assertNotIn(str(target), json.dumps(public_command))
        self.assertIn("<operator-cidfile>", public_command)
        self.assertIn(
            "type=bind,src=<public-runtime-input>,"
            "dst=/challenge/input/inputs.json,readonly",
            public_command,
        )
        self.assertIn(
            "type=bind,src=<benchmark-output>,dst=/challenge/output",
            public_command,
        )

    def test_proposal_records_native_status_but_stays_provisional(self) -> None:
        repetition = {
            "elapsed_seconds": 9.0,
            "max_rss_bytes": 9 * 1024,
            "output_bytes": 9 * 512,
        }
        container_repetition = {
            "elapsed_seconds": 1.0,
            "max_rss_bytes": 1024,
            "output_bytes": 512,
        }
        results = [{"repetitions": [repetition]}]
        cgroup = {
            "cpu_stat": {
                "usage_usec": 100,
                "user_usec": 80,
                "system_usec": 20,
                "nr_periods": 10,
                "nr_throttled": 1,
                "throttled_usec": 5,
            },
            "cpu_usage_delta_usec": 50,
            "memory_peak_bytes": 4096,
            "memory_events": {"max": 0, "oom": 0, "oom_kill": 0},
            "pids_peak": 2,
            "pids_current": 1,
            "pids_events": {"max": 0},
        }
        container_results = [
            {
                "repetitions": [
                    {
                        **container_repetition,
                        "cgroup_v2": cgroup,
                    }
                ]
            }
        ]
        container = {
            "runtime_available": False,
            "runtime_server": None,
            "runtime_context": None,
        }
        proposal = benchmark_runtime._proposal(
            results,
            container_results,
            container,
            apple_silicon_colima_completed=True,
        )
        self.assertTrue(
            proposal["validation"]["apple_silicon_colima_completed"]
        )
        self.assertFalse(
            proposal["validation"]["representative_router_validation_completed"]
        )
        self.assertEqual(
            "provisional-not-final",
            proposal["platform_proposal"]["status"],
        )
        self.assertIn(
            "최종 자원 한도",
            " ".join(proposal["limitations"]),
        )
        observed = proposal["reference_router_observed_max"][
            "cgroup_v2_observed_max"
        ]
        self.assertEqual(4096, observed["memory_peak_bytes"])
        self.assertEqual(2, observed["pids_peak"])
        reference_max = proposal["reference_router_observed_max"]
        self.assertEqual("container-only", reference_max["source"])
        self.assertEqual(1.0, reference_max["elapsed_seconds"])
        self.assertEqual(1024, reference_max["max_rss_bytes"])
        self.assertNotIn(
            "cgroup memory.peak",
            proposal["reference_router_observed_max"][
                "not_measured_by_process_worker"
            ],
        )

    def test_proposal_records_representative_router_validation(self) -> None:
        cgroup = {
            "cpu_stat": {
                "usage_usec": 100,
                "user_usec": 80,
                "system_usec": 20,
                "nr_periods": 10,
                "nr_throttled": 0,
                "throttled_usec": 0,
            },
            "cpu_usage_delta_usec": 50,
            "memory_peak_bytes": 4096,
            "memory_events": {"max": 0, "oom": 0, "oom_kill": 0},
            "pids_peak": 2,
            "pids_current": 1,
            "pids_events": {"max": 0},
        }
        repetition = {
            "elapsed_seconds": 1.0,
            "max_rss_bytes": 4096,
            "output_bytes": 512,
            "cgroup_v2": cgroup,
        }
        container_results = [
            {
                "strategy": strategy,
                "deterministic": True,
                "id_and_order_invariant": True,
                "repetitions": [repetition],
            }
            for strategy in benchmark_runtime.REPRESENTATIVE_STRATEGIES
        ]
        proposal = benchmark_runtime._proposal(
            [],
            container_results,
            {
                "runtime_available": True,
                "runtime_server": {"Os": "linux", "Arch": "arm64"},
                "runtime_context": "colima",
            },
            apple_silicon_colima_completed=True,
        )
        self.assertTrue(
            proposal["validation"]["representative_router_validation_completed"]
        )
        self.assertFalse(
            proposal["validation"]["final_runtime_boundary_validation_completed"]
        )
        self.assertEqual(
            "provisional-awaiting-final-runtime-boundary-validation",
            proposal["status"],
        )
        final_proposal = benchmark_runtime._proposal(
            [],
            container_results,
            {
                "runtime_available": True,
                "runtime_server": {"Os": "linux", "Arch": "arm64"},
                "runtime_context": "colima",
            },
            apple_silicon_colima_completed=True,
            final_runtime_boundary_validation_completed=True,
        )
        self.assertEqual("final-frozen", final_proposal["status"])
        self.assertEqual(
            "final",
            final_proposal["platform_proposal"]["status"],
        )
        self.assertTrue(
            final_proposal["validation"][
                "final_runtime_boundary_validation_completed"
            ]
        )

    def test_native_result_validity_fails_closed(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "결정성"):
            benchmark_runtime._require_official_result_validity(
                [
                    {
                        "strategy": "prompt-heuristic",
                        "tier": "fast",
                        "deterministic": False,
                        "id_and_order_invariant": True,
                    }
                ]
            )

    def test_summary_includes_cgroup_observed_max(self) -> None:
        cgroup = {
            "cpu_stat": {
                "usage_usec": 100,
                "nr_throttled": 2,
                "throttled_usec": 7,
            },
            "cpu_usage_delta_usec": 50,
            "memory_peak_bytes": 4096,
            "pids_peak": 3,
        }
        summary = benchmark_runtime._summary_stats(
            [
                {
                    "elapsed_seconds": 1.0,
                    "cpu_seconds": 0.5,
                    "max_rss_bytes": 2048,
                    "processes": 1,
                    "threads": 1,
                    "output_bytes": 512,
                    "cgroup_v2": cgroup,
                }
            ]
        )
        self.assertEqual(
            {
                "cpu_usage_delta_usec": 50,
                "cpu_usage_usec": 100,
                "cpu_nr_throttled": 2,
                "cpu_throttled_usec": 7,
                "memory_peak_bytes": 4096,
                "pids_peak": 3,
            },
            summary["cgroup_v2_observed_max"],
        )

    def test_report_pair_rolls_back_when_second_publish_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = pathlib.Path(temporary)
            json_output = target / "report.json"
            markdown_output = target / "report.md"
            json_output.write_text("old-json", encoding="utf-8")
            markdown_output.write_text("old-markdown", encoding="utf-8")
            real_replace = benchmark_runtime.os.replace

            def replace(source, destination):
                if (
                    pathlib.Path(source).suffix == ".staged"
                    and pathlib.Path(destination) == markdown_output
                ):
                    raise OSError("injected publish failure")
                return real_replace(source, destination)

            with mock.patch.object(
                benchmark_runtime,
                "_markdown",
                return_value="new-markdown",
            ), mock.patch.object(
                benchmark_runtime.os,
                "replace",
                side_effect=replace,
            ):
                with self.assertRaisesRegex(OSError, "injected"):
                    benchmark_runtime._publish_report_pair(
                        json_output=json_output,
                        markdown_output=markdown_output,
                        report={"schema_version": 3},
                    )
            self.assertEqual(
                "old-json",
                json_output.read_text(encoding="utf-8"),
            )
            self.assertEqual(
                "old-markdown",
                markdown_output.read_text(encoding="utf-8"),
            )
            self.assertEqual(
                {"report.json", "report.md"},
                {path.name for path in target.iterdir()},
            )

    def test_report_pair_rejects_stale_crash_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = pathlib.Path(temporary)
            json_output = target / "report.json"
            markdown_output = target / "report.md"
            marker = benchmark_runtime._report_transaction_marker_path(
                json_output,
                markdown_output,
            )
            marker.write_text('{"status":"publish-in-progress"}\n', encoding="utf-8")
            with mock.patch.object(
                benchmark_runtime,
                "_markdown",
                return_value="new-markdown",
            ):
                with self.assertRaisesRegex(RuntimeError, "transaction marker"):
                    benchmark_runtime._publish_report_pair(
                        json_output=json_output,
                        markdown_output=markdown_output,
                        report={"schema_version": 3},
                    )
            self.assertTrue(marker.exists())
            self.assertFalse(json_output.exists())
            self.assertFalse(markdown_output.exists())


if __name__ == "__main__":
    unittest.main()
