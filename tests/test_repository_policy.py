# SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
# SPDX-License-Identifier: Apache-2.0

"""Repository structure, licensing, and participant-document checks."""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
import statistics
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
COPYRIGHT_TAG = "SPDX-File" + "CopyrightText:"
LICENSE_TAG = "SPDX-License-" + "Identifier:"

CANONICAL_LICENSE_HASHES = {
    "LICENSE": "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30",
    "LICENSES/Apache-2.0.txt": (
        "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"
    ),
}

REQUIRED_FILES = {
    "CONTRIBUTING.md",
    "DATA_LICENSES.md",
    "DEVELOPING.md",
    "LICENSE",
    "MANIFEST.in",
    "NOTICE",
    "pyproject.toml",
    "README.md",
    "REUSE.toml",
    "setup.cfg",
    "configs/routing-policy.v1.json",
    "container/BASE_IMAGE.md",
    "container/Dockerfile",
    "container/measurement.Dockerfile",
    "container/entrypoint.py",
    "docs/CHALLENGE_RULES.md",
    "docs/APPLE_SILICON_MEASUREMENT.md",
    "docs/DATA_CARD.md",
    "docs/ENFORCEMENT.md",
    "docs/OPERATIONS.md",
    "docs/RUNTIME.md",
    "docs/REVIEW_GUIDE.md",
    "docs/SCORING.md",
    "docs/SUBMISSION.md",
    "docs/TIEBREAK_LATENCY.md",
    "docs/runtime-benchmark.json",
    "docs/runtime-benchmark.md",
    "baselines/README.md",
    "baselines/feature_budget.py",
    "baselines/hash_regex.py",
    "baselines/prompt_heuristic.py",
    "baselines/requirements-train.txt",
    "baselines/train_hash_regex.py",
    "schemas/input.v1.schema.json",
    "schemas/outcome.v1.schema.json",
    "schemas/policy.v1.schema.json",
    "schemas/submission.v1.schema.json",
    "schemas/technical-submission.v1.schema.json",
    "src/ossp_router/cli.py",
    "src/ossp_router/heuristic.py",
    "src/ossp_router/image_evidence.py",
    "src/ossp_router/operator_helper.py",
    "src/ossp_router/orchestrator.py",
    "src/ossp_router/protocol.py",
    "src/ossp_router/public_runtime.py",
    "src/ossp_router/resources/routing-policy.v1.json",
    "src/ossp_router/runtime.py",
    "src/ossp_router/scoring.py",
    "src/ossp_router/tiebreak_latency.py",
    "tests/test_operator_helper.py",
    "tests/test_orchestrator.py",
    "tests/test_check_runtime.py",
    "tests/test_feature_budget_baseline.py",
    "tests/test_hash_regex_baseline.py",
    "tests/test_prompt_heuristic.py",
    "tests/test_public_runtime.py",
    "tests/test_retry_policy.py",
    "tests/test_runtime.py",
    "tests/test_tiebreak_latency.py",
    "tools/benchmark_runtime.py",
    "tools/check_runtime.py",
    "tools/validate_technical_submission.py",
}

DATA_CONFIGURATIONS = {
    "AIME public development material",
    "BABILong 4K/16K",
    "Belebele Korean",
    "CRUXEval input prediction",
    "CRUXEval output prediction",
    "DeepMind Mathematics extrapolation",
    "DeepMind Mathematics interpolation",
    "GSM8K",
    "HRMCR",
    "RuleTaker",
    "TruthfulQA binary",
}

LOCAL_GENERATED_PREFIXES = (
    (".git",),
    (".ruff_cache",),
    (".venv",),
    (".venv-data",),
    ("build",),
    ("dist",),
    ("data", "cache"),
    ("data", "materialized"),
)


def _normalized_document(path: pathlib.Path) -> str:
    """Return prose with Markdown line wrapping removed for policy checks."""

    return re.sub(r"\s+", " ", path.read_text(encoding="utf-8")).strip()


def _missing_semantic_markers(text: str, markers: tuple[str, ...]) -> list[str]:
    return [
        marker
        for marker in markers
        if re.sub(r"\s+", " ", marker).strip() not in text
    ]


def _is_local_generated(relative: pathlib.Path) -> bool:
    return any(
        relative.parts[: len(prefix)] == prefix
        for prefix in LOCAL_GENERATED_PREFIXES
    )


class RepositoryPolicyTest(unittest.TestCase):
    def test_required_files_exist(self) -> None:
        missing = sorted(path for path in REQUIRED_FILES if not (ROOT / path).is_file())
        self.assertEqual([], missing)
        self.assertFalse((ROOT / "docs/RELEASE_BLOCKERS.md").exists())

    def test_canonical_license_texts_are_unmodified(self) -> None:
        for relative_path, expected_hash in CANONICAL_LICENSE_HASHES.items():
            with self.subTest(path=relative_path):
                digest = hashlib.sha256((ROOT / relative_path).read_bytes()).hexdigest()
                self.assertEqual(expected_hash, digest)

    def test_notice_describes_released_public_data(self) -> None:
        notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
        self.assertIn("public Train/Dev prompt adaptations", notice)
        self.assertIn("THIRD_PARTY_NOTICES.md", notice)
        self.assertNotIn("only independently authored toy inputs", notice)

    def test_commentable_files_have_spdx_tags(self) -> None:
        candidates = []
        for path in ROOT.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(ROOT)
            if _is_local_generated(relative):
                continue
            if any(part.endswith(".egg-info") for part in relative.parts):
                continue
            if relative.parts[0] == "LICENSES" or relative.as_posix() in {
                "LICENSE",
                "NOTICE",
            }:
                continue
            if path.suffix in {".cfg", ".md", ".py", ".toml"} or path.name == (
                ".gitignore"
            ):
                candidates.append(path)

        missing = []
        for path in candidates:
            text = path.read_text(encoding="utf-8")
            if COPYRIGHT_TAG not in text or LICENSE_TAG not in text:
                missing.append(path.relative_to(ROOT).as_posix())
        self.assertEqual([], sorted(missing))

    def test_data_matrix_covers_all_task_configurations(self) -> None:
        text = (ROOT / "DATA_LICENSES.md").read_text(encoding="utf-8")
        missing = sorted(name for name in DATA_CONFIGURATIONS if name not in text)
        self.assertEqual([], missing)
        self.assertIn("Apache-2.0 AND BSD-3-Clause", text)
        self.assertEqual(1, text.count("Source-fetch-only"))

    def test_policy_values_match_participant_docs(self) -> None:
        policy_path = ROOT / "configs/routing-policy.v1.json"
        bundled_path = ROOT / "src/ossp_router/resources/routing-policy.v1.json"
        self.assertEqual(policy_path.read_bytes(), bundled_path.read_bytes())
        self.assertEqual(
            "a2f467eb738a6e56fee991743bc4af8ad1866223223fc1b594b9172d8043048a",
            hashlib.sha256(policy_path.read_bytes()).hexdigest(),
        )
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        self.assertEqual(1, policy["schema_version"])
        self.assertEqual(1_000_000, policy["token_unit"])
        self.assertEqual(32_768, policy["context_limit_tokens"])
        self.assertNotIn("challenge_id", policy)
        self.assertNotIn("warning_threshold", policy)
        for document in ("README.md", "docs/SCORING.md"):
            text = (ROOT / document).read_text(encoding="utf-8")
            for tier, expected_weight in (
                ("fast", "0.4"),
                ("balanced", "0.3"),
                ("premium", "0.3"),
            ):
                self.assertEqual(
                    expected_weight, str(float(policy["tiers"][tier]["weight"]))
                )
                self.assertIn(expected_weight, text)

    def test_participant_docs_do_not_expose_internal_release_terms(self) -> None:
        forbidden = (
            "RELEASE_BLOCKERS",
            "clean-room",
            "/" + "Users/",
            "private-" + "results",
            "입력 계약",
            "출력 계약",
            "결과 계약",
            "wire schema",
            "canonical SHA",
            "변경 불가능한 commit SHA",
        )
        documents = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]
        findings = []
        for document in documents:
            text = document.read_text(encoding="utf-8")
            for needle in forbidden:
                if needle in text:
                    findings.append(f"{document.relative_to(ROOT)}: {needle}")
        self.assertEqual([], findings)

    def test_rules_cover_runtime_audit_and_submission_boundaries(self) -> None:
        rules = _normalized_document(ROOT / "docs/CHALLENGE_RULES.md")
        required_markers = (
            "제출 소스, 의존성, 컨테이너",
            "문항 ID와 순서",
            "공개 Train/Dev의 프롬프트와 모델별 평가 결과",
            "동일한 라우터 프로그램과 학습 파일",
            "비공개 Git submodule",
            "제출한 커밋에서 컨테이너 이미지",
            "제출 커밋과 컨테이너 이미지 다이제스트",
            "변경 가능한 이미지 태그",
            "`challenge_id`, `split`과 `episode_id`는",
            "모델 선택, 정책 전환, 학습 자료 조회",
            "프롬프트 해시를 사용하는 공개 자료 조회",
            "공개 비용 계수와 토큰 수",
            "문항별 모델 평가 결과나 실제 비용",
            "공개되지 않은 평가 자료",
            "다시 강제하는 입력 한도",
            "`Apache-2.0`, `MIT`, `BSD-2-Clause`",
            "목록에 없는 copyleft",
            "사용자 정의 라이선스",
            "기반 운영체제와 언어 런타임의 표준 구성요소",
            "심사에 필요한 전체 소스코드",
            "가중치를 공개한 모델",
            "수상일로부터 5년",
            "모델 답변을 실시간으로 받아 비교",
            "네트워크 없이 `linux/arm64`",
        )
        self.assertEqual(
            [], sorted(_missing_semantic_markers(rules, required_markers))
        )

        submission = _normalized_document(ROOT / "docs/SUBMISSION.md")
        for marker in (
            "2026년 7월 18일부터 8월 27일 18:00",
            "https://osscontest.kr/",
            "최종 제출 시점부터 평가가 끝날 때까지",
            "`submission-ossp-skt.json`",
            "validate_technical_submission.py",
            "저장소의 루트에 반드시 커밋",
            "`프로젝트 등록 URL`",
            "정확한 커밋 스냅샷 URL",
            "대회 사이트에 별도로 업로드하지",
            "원본 파일 1개",
            "PDF 1개",
            "수상작",
            "공식 제출이나 심사 조건이 아닙니다",
            "복수로 제출",
            "마지막으로 접수된 파일",
            "가중치를 공개한 모델",
            "수상일로부터 5년",
        ):
            self.assertIn(marker, submission)

        technical_schema = json.loads(
            (ROOT / "schemas/technical-submission.v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertFalse(technical_schema["additionalProperties"])
        self.assertEqual(
            {
                "schema_version",
                "challenge_id",
                "repository_url",
                "commit_sha",
                "image_digest",
                "primary_license",
            },
            set(technical_schema["required"]),
        )
        self.assertEqual(
            "ossp-2026-llm-router-challenge",
            technical_schema["properties"]["challenge_id"]["const"],
        )
        self.assertEqual(
            [
                "Apache-2.0",
                "MIT",
                "BSD-2-Clause",
                "BSD-3-Clause",
                "ISC",
                "0BSD",
            ],
            technical_schema["properties"]["primary_license"]["enum"],
        )
        patterns = {
            name: re.compile(technical_schema["properties"][name]["pattern"])
            for name in ("repository_url", "commit_sha", "image_digest")
        }
        self.assertIsNotNone(
            patterns["repository_url"].fullmatch(
                "https://github.com/example/router"
            )
        )
        self.assertIsNotNone(patterns["commit_sha"].fullmatch("0" * 40))
        self.assertIsNotNone(
            patterns["image_digest"].fullmatch(
                "ghcr.io/example/router@sha256:" + "0" * 64
            )
        )
        for name, value in (
            ("repository_url", "http://github.com/example/router"),
            ("commit_sha", "0" * 39),
            ("commit_sha", "0" * 40 + "x"),
            ("image_digest", "ghcr.io/example/router:latest"),
            ("image_digest", "sha256:" + "0" * 64),
        ):
            with self.subTest(name=name, value=value):
                self.assertIsNone(patterns[name].fullmatch(value))

        scoring = _normalized_document(ROOT / "docs/SCORING.md")
        for marker in (
            "공식 실행 레이턴시가 낮은 순서",
            "모델 추론이나 답변 비교 시간이 아니라",
            "반올림하기 전의 정확한 `Decimal` 최종 점수",
            "기록에 포함하지 않는 준비 실행 한 번 뒤 5회",
            "Fast·Balanced·Premium 중앙값을 합산",
            "운영자 장애로 무효가 된 반복은 버리고",
        ):
            self.assertIn(marker, scoring)

        container = _normalized_document(ROOT / "container/README.md")
        for marker in (
            "라우터 실행 입력 JSON의 컨테이너 내부",
            "선택 결과 JSON의 경로",
            "제한 시간 초과와 비정상 종료",
            "CPU, RAM, 프로세스·스레드 수",
            "읽기 전용 파일 시스템",
            "운영자 측 기술 장애",
        ):
            self.assertIn(marker, container)

        enforcement = _normalized_document(ROOT / "docs/ENFORCEMENT.md")
        for marker in (
            "최초 실행을 포함하여 최대 3회",
            "첫 번째 유효한 결과",
            "공식 실행 횟수에 포함하지 않습니다",
            "전체 제출 실격 사유",
            "예산 초과는 참가 자격이나 공정성 위반이 아닙니다",
            "자동 실격하지 않고 운영자가 근거를 검토",
        ):
            self.assertIn(marker, enforcement)

        runtime = _normalized_document(ROOT / "docs/RUNTIME.md")
        for marker in (
            "/challenge/input/inputs.json",
            "/challenge/output/submission.json",
            "SIGTERM",
            "SIGKILL",
            "--log-driver none",
            "공식 평가에 적용하는 최종 자원 한도",
        ):
            self.assertIn(marker, runtime)

        operations = _normalized_document(ROOT / "docs/OPERATIONS.md")
        for marker in (
            "--pull never",
            "구조화된 실행 기록의 `status`",
            "원본 입력을 이 비공개 작업 경로에",
            "ID·순서 변경 감사를 생략하는 옵션을",
            "--private-work-directory",
            "운영자가 소유하고 권한이 정확히 `0700`",
            "결과 출력 경로와 겹치지 않는",
            "`cleanup_attempt_id`",
            "review_required",
            "inconclusive_infrastructure",
            "종료 코드 `5`",
            "자동 채점과 자동 재시도를 중단",
            "`audit_skipped`",
            "submission_preflight_rejected",
            "declared_volume_not_allowed",
        ):
            self.assertIn(marker, operations)

    def test_apple_silicon_measurement_policy_is_fail_closed(self) -> None:
        measurement = (
            ROOT / "docs/APPLE_SILICON_MEASUREMENT.md"
        ).read_text(encoding="utf-8")
        for phrase in (
            "Apple Silicon 장비",
            "`linux/arm64`",
            "Colima 0.10.3",
            "cgroup v2",
            "공개 Train/Dev 전체",
            "I_ATTEST_APPLE_SILICON_COLIMA",
            "I_ATTEST_FINAL_RUNTIME_BOUNDARIES_PASSED",
            "대표적인 프롬프트 특징 기반 구현",
            "동점 레이턴시",
        ):
            self.assertIn(phrase, measurement)

    def test_runtime_benchmark_report_is_internally_consistent(self) -> None:
        report = json.loads(
            (ROOT / "docs/runtime-benchmark.json").read_text(encoding="utf-8")
        )
        self.assertEqual(3, report["schema_version"])
        self.assertEqual(
            "reference-router-runtime-benchmark",
            report["report_type"],
        )
        self.assertEqual("apple-silicon-colima", report["measurement_mode"])
        self.assertEqual(
            "apple-silicon-colima-reference-completed",
            report["measurement_status"],
        )
        environment_evidence = report["apple_silicon_environment_evidence"]
        self.assertEqual("Darwin", environment_evidence["host"]["system"])
        self.assertIn(
            environment_evidence["host"]["machine"],
            ("arm64", "aarch64"),
        )
        self.assertEqual(
            "aarch64",
            environment_evidence["colima"]["architecture"],
        )
        self.assertEqual(
            "2",
            environment_evidence["docker"]["cgroup_version"],
        )
        self.assertEqual(2_640, report["input"]["episodes"])
        self.assertEqual(5, report["repetitions"])
        self.assertFalse(
            report["measurement_methodology"][
                "page_cache_dropped_between_repetitions"
            ]
        )
        self.assertEqual(
            "sha256",
            report["source_tree_manifest"]["algorithm"],
        )
        self.assertEqual(
            report["source_tree_manifest"]["sha256"],
            report["resolved_image"]["source_manifest_sha256"],
        )
        self.assertEqual(
            {"os": "linux", "architecture": "arm64"},
            report["resolved_image"]["platform"],
        )
        expected_pairs = {
            (strategy, tier)
            for strategy in (
                "always-light",
                "prompt-heuristic",
                "feature-budget",
                "hash-regex",
            )
            for tier in ("fast", "balanced", "premium")
        }
        for collection_name in ("results", "container_results"):
            results = report[collection_name]
            self.assertEqual(
                expected_pairs,
                {(result["strategy"], result["tier"]) for result in results},
            )
            for result in results:
                repetitions = result["repetitions"]
                self.assertEqual(report["repetitions"], len(repetitions))
                self.assertTrue(result["deterministic"])
                self.assertTrue(result["id_and_order_invariant"])
                self.assertEqual(
                    1,
                    len({item["output_sha256"] for item in repetitions}),
                )
                summary = result["summary"]
                elapsed = [item["elapsed_seconds"] for item in repetitions]
                cpu = [item["cpu_seconds"] for item in repetitions]
                self.assertEqual(min(elapsed), summary["elapsed_seconds_min"])
                self.assertEqual(
                    statistics.median(elapsed),
                    summary["elapsed_seconds_median"],
                )
                self.assertEqual(max(elapsed), summary["elapsed_seconds_max"])
                self.assertEqual(min(cpu), summary["cpu_seconds_min"])
                self.assertEqual(
                    statistics.median(cpu),
                    summary["cpu_seconds_median"],
                )
                self.assertEqual(max(cpu), summary["cpu_seconds_max"])
                self.assertEqual(
                    max(item["max_rss_bytes"] for item in repetitions),
                    summary["max_rss_bytes"],
                )
                self.assertEqual(
                    max(item["output_bytes"] for item in repetitions),
                    summary["output_bytes"],
                )
        repetitions = [
            item
            for result in report["container_results"]
            for item in result["repetitions"]
        ]
        observed = report["resource_limit_proposal"][
            "reference_router_observed_max"
        ]
        self.assertEqual(
            max(item["elapsed_seconds"] for item in repetitions),
            observed["elapsed_seconds"],
        )
        self.assertEqual(
            max(item["max_rss_bytes"] for item in repetitions),
            observed["max_rss_bytes"],
        )
        self.assertEqual(
            max(item["output_bytes"] for item in repetitions),
            observed["output_bytes"],
        )
        self.assertTrue(observed["elapsed_rss_output_within_candidate_profile"])
        measurement = report["measurement_profile"]["container"]
        self.assertEqual(2, measurement["cpu_cores"])
        self.assertEqual(2 * 1024**3, measurement["memory_bytes"])
        self.assertEqual(
            measurement["memory_bytes"],
            measurement["memory_swap_total_bytes"],
        )
        self.assertEqual(32, measurement["pids_limit"])
        self.assertEqual(
            256 * 1024**2,
            measurement["temporary_storage_bytes"],
        )
        self.assertEqual("private", measurement["cgroup_namespace"])
        self.assertEqual("none", measurement["network"])
        self.assertIsInstance(
            report["container"]["runtime_rootfs_apparent_is_lower_bound"],
            bool,
        )
        benchmark_markdown = (
            ROOT / "docs/runtime-benchmark.md"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "Apple Silicon 호스트·Colima·linux/arm64 대표 측정",
            benchmark_markdown,
        )
        self.assertIn("공식 플랫폼 참고 측정 조건", benchmark_markdown)
        self.assertNotIn(
            "아래의 새 잠정 프로필로 다시 측정한 수치가 아닙니다",
            benchmark_markdown,
        )
        proposal = report["resource_limit_proposal"]
        self.assertEqual("final-frozen", proposal["status"])
        self.assertEqual("final", proposal["platform_proposal"]["status"])
        self.assertTrue(
            proposal["validation"]["apple_silicon_colima_completed"]
        )
        self.assertTrue(
            proposal["validation"][
                "representative_router_validation_completed"
            ]
        )
        self.assertTrue(
            proposal["validation"][
                "final_runtime_boundary_validation_completed"
            ]
        )
        boundary = report["measurement_methodology"][
            "final_runtime_boundary_validation"
        ]
        self.assertEqual("Ran 83 tests; OK", boundary["observed_result"])

    def test_relative_markdown_links_resolve(self) -> None:
        pattern = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
        broken = []
        for document in ROOT.rglob("*.md"):
            if _is_local_generated(document.relative_to(ROOT)):
                continue
            text = document.read_text(encoding="utf-8")
            for target in pattern.findall(text):
                if target.startswith(("http://", "https://", "mailto:", "#")):
                    continue
                relative_target = target.split("#", 1)[0]
                resolved = (document.parent / relative_target).resolve()
                if not resolved.exists():
                    broken.append(
                        f"{document.relative_to(ROOT).as_posix()} -> {target}"
                    )
        self.assertEqual([], sorted(broken))

    def test_sensitive_and_materialized_paths_are_ignored(self) -> None:
        text = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        for pattern in (
            "/.local-data/",
            "/.venv-data/",
            "/operator-state/",
            "/data/cache/",
            "/data/materialized/",
            "*.key",
            "*.pem",
        ):
            self.assertIn(pattern, text)
        self.assertNotIn("*.json", text)

        dockerignore = (ROOT / ".dockerignore").read_text(
            encoding="utf-8"
        ).splitlines()
        expected_dockerignore = [
            "# SPDX-" + "FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.",
            "# SPDX-" + "License-Identifier: Apache-2.0",
            "",
            "**",
            "!src/",
            "src/**",
            "!src/ossp_router/",
            "src/ossp_router/**",
            "!src/ossp_router/__init__.py",
            "!src/ossp_router/cli.py",
            "!src/ossp_router/heuristic.py",
            "!src/ossp_router/image_evidence.py",
            "!src/ossp_router/operator_helper.py",
            "!src/ossp_router/orchestrator.py",
            "!src/ossp_router/protocol.py",
            "!src/ossp_router/runtime.py",
            "!src/ossp_router/scoring.py",
            "!src/ossp_router/resources/",
            "src/ossp_router/resources/**",
            "!src/ossp_router/resources/__init__.py",
            "!src/ossp_router/resources/routing-policy.v1.json",
            "!baselines/",
            "baselines/**",
            "!baselines/feature_budget.py",
            "!baselines/hash_regex.py",
            "!baselines/hash-regex-public.v1.json",
            "!container/",
            "container/**",
            "!container/Dockerfile",
            "!container/measurement.Dockerfile",
            "!container/entrypoint.py",
        ]
        self.assertEqual(expected_dockerignore, dockerignore)

    def test_public_tree_has_no_internal_paths_secrets_or_model_artifacts(
        self,
    ) -> None:
        text_files = []
        for path in ROOT.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(ROOT)
            if _is_local_generated(relative):
                continue
            if "__pycache__" in relative.parts or path.suffix in {".pyc", ".pyo"}:
                continue
            if any(part.endswith(".egg-info") for part in relative.parts):
                continue
            text_files.append(path)

        forbidden_text = (
            "/" + "Users/",
            "private-" + "results",
            "release-" + "planning",
            "INTERNAL_" + "RELEASE_CHECKLIST",
            "BEGIN " + "PRIVATE KEY",
        )
        secret_patterns = (
            re.compile(r"AKIA[0-9A-Z]{16}"),
            re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
            re.compile(r"hf_[A-Za-z0-9]{20,}"),
            re.compile(r"sk-[A-Za-z0-9]{20,}"),
            re.compile(r"xox[baprs]-[A-Za-z0-9-]{20,}"),
        )
        findings = []
        for path in text_files:
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                findings.append(
                    f"binary:{path.relative_to(ROOT).as_posix()}"
                )
                continue
            for needle in forbidden_text:
                if needle in text:
                    findings.append(
                        f"{path.relative_to(ROOT).as_posix()}:{needle}"
                    )
            for pattern in secret_patterns:
                if pattern.search(text):
                    findings.append(
                        f"{path.relative_to(ROOT).as_posix()}:{pattern.pattern}"
                    )
        self.assertEqual([], sorted(findings))

        model_suffixes = {
            ".bin",
            ".npy",
            ".npz",
            ".onnx",
            ".pickle",
            ".pkl",
            ".pt",
            ".safetensors",
        }
        model_artifacts = sorted(
            path.relative_to(ROOT).as_posix()
            for path in text_files
            if path.suffix.lower() in model_suffixes
        )
        self.assertEqual([], model_artifacts)

    def test_phase_c_policy_does_not_change_frozen_v1(self) -> None:
        policy = json.loads(
            (ROOT / "configs/routing-policy.v1.json").read_text(encoding="utf-8")
        )
        self.assertEqual(1, policy["schema_version"])
        self.assertEqual("ossp-2026-prompt-router-v1", policy["policy_id"])
        dockerfile = (ROOT / "container/Dockerfile").read_text(encoding="utf-8")
        self.assertRegex(
            dockerfile,
            r"FROM python:3\.11\.15-alpine3\.23@sha256:[0-9a-f]{64}",
        )
        self.assertIn(
            'io.sktelecom.ossp.source-manifest-sha256="${SOURCE_MANIFEST_SHA256}"',
            dockerfile,
        )

    def test_reference_images_remove_unused_packaging_tools(self) -> None:
        for relative_path in (
            "container/Dockerfile",
            "container/measurement.Dockerfile",
        ):
            dockerfile = (ROOT / relative_path).read_text(encoding="utf-8")
            self.assertIn(
                "RUN python3 -m pip uninstall --yes pip setuptools wheel",
                dockerfile,
            )

    def test_operator_helper_image_is_immutable_and_separate(self) -> None:
        runtime = (ROOT / "src/ossp_router/runtime.py").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            '"python:3.14.6-alpine3.23@sha256:"',
            runtime,
        )
        self.assertIn(
            '"b165067c5afc37fa5608a3c05609cc3d51aafd808a30fbfd822ee594fef55ad4"',
            runtime,
        )

    def test_code_distribution_includes_only_project_license(self) -> None:
        setup = (ROOT / "setup.cfg").read_text(encoding="utf-8")
        manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
        for text in (setup, manifest):
            self.assertIn("LICENSES/Apache-2.0.txt", text)
            self.assertNotIn("LICENSES/*.txt", text)

    def test_repository_contains_no_symbolic_links(self) -> None:
        links = sorted(
            path.relative_to(ROOT).as_posix()
            for path in ROOT.rglob("*")
            if path.is_symlink()
            and not _is_local_generated(path.relative_to(ROOT))
            and not any(
                part.endswith(".egg-info")
                for part in path.relative_to(ROOT).parts
            )
        )
        self.assertEqual([], links)


if __name__ == "__main__":
    unittest.main()
