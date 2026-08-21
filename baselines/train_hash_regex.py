# SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
# SPDX-License-Identifier: Apache-2.0

"""Train the public hash-regex router without retaining per-episode rows."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

try:
    import numpy as np
except ImportError:  # pragma: no cover - exercised by the CLI error path
    np = None

import hash_regex
from ossp_router.protocol import (
    MODEL_IDS,
    TIERS,
    Decision,
    InputBatch,
    Outcome,
    OutcomeBatch,
    ProtocolError,
    RoutingPolicy,
    Submission,
    load_bundled_policy,
    load_input,
    load_outcomes,
    load_policy,
    policy_sha256,
)
from ossp_router.scoring import score_submissions


def _require_numpy() -> None:
    if np is None:
        raise RuntimeError(
            "학습에는 NumPy가 필요합니다. "
            "baselines/requirements-train.txt를 설치해 주세요."
        )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _outcome_cost(outcome: Outcome, policy: RoutingPolicy) -> float:
    rates = policy.models[outcome.model_id]
    unit = Decimal(policy.token_unit)
    cost = (
        rates.fixed_cost
        + Decimal(outcome.input_tokens) * rates.input_token_rate / unit
        + Decimal(outcome.output_tokens) * rates.output_token_rate / unit
    )
    result = float(cost)
    if not math.isfinite(result) or result <= 0:
        raise ProtocolError("학습 outcome의 모델 비용은 0보다 커야 합니다.")
    return result


def _training_matrix(
    inputs: InputBatch,
    outcomes: OutcomeBatch,
    policy: RoutingPolicy,
    hash_bins: int,
) -> Tuple[Any, Any]:
    _require_numpy()
    if inputs.schema_version != outcomes.schema_version:
        raise ProtocolError("Train 입력과 outcome의 schema_version이 다릅니다.")
    if inputs.challenge_id != outcomes.challenge_id or inputs.split != outcomes.split:
        raise ProtocolError("Train 입력과 outcome의 실행 메타데이터가 다릅니다.")
    outcome_index = {
        (outcome.episode_id, outcome.model_id): outcome
        for outcome in outcomes.outcomes
    }
    expected = {
        (episode.episode_id, model_id)
        for episode in inputs.episodes
        for model_id in MODEL_IDS
    }
    if set(outcome_index) != expected:
        raise ProtocolError("Train outcome 행렬이 입력과 모델 전체를 포함하지 않습니다.")
    matrix = np.asarray(
        [
            hash_regex.raw_feature_vector(episode, hash_bins)
            for episode in inputs.episodes
        ],
        dtype=np.float64,
    )
    targets = []
    for episode in inputs.episodes:
        rows = [outcome_index[(episode.episode_id, model_id)] for model_id in MODEL_IDS]
        scores = [float(row.score) for row in rows]
        log_costs = [math.log(_outcome_cost(row, policy)) for row in rows]
        targets.append(scores + log_costs)
    return matrix, np.asarray(targets, dtype=np.float64)


def _fit_ridge(matrix: Any, targets: Any, alpha: float) -> Tuple[Any, Any, Any, Any]:
    _require_numpy()
    mean = matrix.mean(axis=0)
    scale = matrix.std(axis=0)
    scale = np.where(scale > 1e-12, scale, 1.0)
    standardized = (matrix - mean) / scale
    intercept = targets.mean(axis=0)
    centered = targets - intercept
    rows, columns = standardized.shape
    if rows <= columns:
        system = standardized @ standardized.T + alpha * np.eye(rows)
        coefficients = standardized.T @ np.linalg.solve(system, centered)
    else:
        system = standardized.T @ standardized + alpha * np.eye(columns)
        coefficients = np.linalg.solve(system, standardized.T @ centered)
    return mean, scale, intercept, coefficients


def _predict_ridge(
    matrix: Any,
    mean: Any,
    scale: Any,
    intercept: Any,
    coefficients: Any,
) -> Any:
    return (matrix - mean) / scale @ coefficients + intercept


def _oof_predictions(
    matrix: Any,
    targets: Any,
    *,
    folds: int,
    alpha: float,
) -> Any:
    _require_numpy()
    rows = matrix.shape[0]
    predictions = np.empty_like(targets)
    fold_ids = np.arange(rows) % folds
    for fold in range(folds):
        validation = fold_ids == fold
        training = ~validation
        mean, scale, intercept, coefficients = _fit_ridge(
            matrix[training], targets[training], alpha
        )
        predictions[validation] = _predict_ridge(
            matrix[validation], mean, scale, intercept, coefficients
        )
    return predictions


def _select_alpha(
    matrix: Any,
    targets: Any,
    *,
    folds: int,
    candidates: Sequence[float],
) -> Tuple[float, Any, Mapping[str, float]]:
    _require_numpy()
    best = None
    diagnostics: Dict[str, float] = {}
    for alpha in candidates:
        predictions = _oof_predictions(
            matrix, targets, folds=folds, alpha=alpha
        )
        score_mse = float(np.mean((predictions[:, : len(MODEL_IDS)] - targets[:, : len(MODEL_IDS)]) ** 2))
        cost_mse = float(np.mean((predictions[:, len(MODEL_IDS) :] - targets[:, len(MODEL_IDS) :]) ** 2))
        objective = score_mse + 0.05 * cost_mse
        diagnostics[format(alpha, ".12g")] = objective
        rank = (objective, alpha)
        if best is None or rank < best[0]:
            best = (rank, alpha, predictions)
    assert best is not None
    return best[1], best[2], diagnostics


def _prediction_rows(predictions: Any) -> Tuple[Sequence[Mapping[str, float]], Sequence[Mapping[str, float]]]:
    scores = []
    costs = []
    model_count = len(MODEL_IDS)
    for row in predictions:
        score_row = {
            model_id: min(1.0, max(0.0, float(row[index])))
            for index, model_id in enumerate(MODEL_IDS)
        }
        cost_row = {
            model_id: math.exp(min(50.0, max(-50.0, float(row[model_count + index]))))
            for index, model_id in enumerate(MODEL_IDS)
        }
        light = cost_row[MODEL_IDS[0]]
        cost_row[MODEL_IDS[1]] = max(cost_row[MODEL_IDS[1]], light * (1.0 + 1e-12))
        cost_row[MODEL_IDS[2]] = max(
            cost_row[MODEL_IDS[2]], cost_row[MODEL_IDS[1]] * (1.0 + 1e-12)
        )
        scores.append(score_row)
        costs.append(cost_row)
    return scores, costs


def _submission(
    inputs: InputBatch,
    policy: RoutingPolicy,
    tier: str,
    selected: Sequence[str],
) -> Submission:
    return Submission(
        schema_version=inputs.schema_version,
        challenge_id=inputs.challenge_id,
        policy_id=policy.policy_id,
        split=inputs.split,
        tier=tier,
        decisions=tuple(
            Decision(episode.episode_id, model_id)
            for episode, model_id in zip(inputs.episodes, selected)
        ),
    )


def _score_one_tier(
    inputs: InputBatch,
    outcomes: OutcomeBatch,
    policy: RoutingPolicy,
    tier: str,
    selected: Sequence[str],
) -> Mapping[str, Any]:
    all_light = tuple(policy.light_model_id for _episode in inputs.episodes)
    submissions = [
        _submission(
            inputs,
            policy,
            candidate,
            selected if candidate == tier else all_light,
        )
        for candidate in TIERS
    ]
    return score_submissions(inputs, outcomes, submissions, policy)["tiers"][tier]


def _safety_candidates(policy: RoutingPolicy, tier: str, size: int) -> Tuple[float, ...]:
    minimum = 1.0 / float(policy.tiers[tier].budget_multiplier)
    if size <= 1 or minimum >= 1.0:
        return (min(1.0, minimum),)
    return tuple(
        minimum + (1.0 - minimum) * index / (size - 1)
        for index in range(size)
    )


def _select_safety_ratios(
    inputs: InputBatch,
    outcomes: OutcomeBatch,
    policy: RoutingPolicy,
    predictions: Any,
    grid_size: int,
) -> Tuple[Mapping[str, float], Mapping[str, Any]]:
    predicted_scores, predicted_costs = _prediction_rows(predictions)
    selected_ratios: Dict[str, float] = {}
    reports: Dict[str, Any] = {}
    for tier in TIERS:
        best = None
        for safety in _safety_candidates(policy, tier, grid_size):
            selected, predicted_ratio = hash_regex.select_models(
                predicted_scores,
                predicted_costs,
                budget_multiplier=float(policy.tiers[tier].budget_multiplier),
                safety_ratio=safety,
            )
            report = _score_one_tier(inputs, outcomes, policy, tier, selected)
            rank = (
                Decimal(report["tier_score"]),
                -Decimal(report["budget_ratio"]),
                -Decimal(str(safety)),
            )
            if best is None or rank > best[0]:
                best = (rank, safety, predicted_ratio, report)
        assert best is not None
        selected_ratios[tier] = best[1]
        reports[tier] = {
            "safety_ratio": best[1],
            "predicted_budget_ratio": best[2],
            "actual_budget_ratio": best[3]["budget_ratio"],
            "tier_score": best[3]["tier_score"],
            "budget_passed": best[3]["budget_passed"],
        }
    return selected_ratios, reports


def _validate_fitted_safety_ratios(
    inputs: InputBatch,
    outcomes: OutcomeBatch,
    policy: RoutingPolicy,
    predictions: Any,
    selected_ratios: Mapping[str, float],
    grid_size: int,
) -> Tuple[Mapping[str, float], Mapping[str, Any]]:
    predicted_scores, predicted_costs = _prediction_rows(predictions)
    validated: Dict[str, float] = {}
    reports: Dict[str, Any] = {}
    for tier in TIERS:
        initial = selected_ratios[tier]
        candidates = {
            initial,
            *(
                value
                for value in _safety_candidates(policy, tier, grid_size)
                if value <= initial
            ),
        }
        chosen = None
        for safety in sorted(candidates, reverse=True):
            selected, predicted_ratio = hash_regex.select_models(
                predicted_scores,
                predicted_costs,
                budget_multiplier=float(policy.tiers[tier].budget_multiplier),
                safety_ratio=safety,
            )
            report = _score_one_tier(inputs, outcomes, policy, tier, selected)
            if report["budget_passed"]:
                chosen = (safety, predicted_ratio, report)
                break
        if chosen is None:
            raise RuntimeError(f"{tier} Train 예산을 통과하는 안전계수가 없습니다.")
        validated[tier] = chosen[0]
        reports[tier] = {
            "oof_selected_safety_ratio": initial,
            "artifact_safety_ratio": chosen[0],
            "predicted_budget_ratio": chosen[1],
            "actual_budget_ratio": chosen[2]["budget_ratio"],
            "tier_score": chosen[2]["tier_score"],
            "budget_passed": chosen[2]["budget_passed"],
        }
    return validated, reports


def _calibrate_validation_safety_ratios(
    inputs: InputBatch,
    outcomes: OutcomeBatch,
    policy: RoutingPolicy,
    artifact: hash_regex.HashRegexArtifact,
    grid_size: int,
) -> Tuple[Mapping[str, float], Mapping[str, Any]]:
    if inputs.schema_version != outcomes.schema_version:
        raise ProtocolError("Dev 입력과 outcome의 schema_version이 다릅니다.")
    if inputs.challenge_id != outcomes.challenge_id or inputs.split != outcomes.split:
        raise ProtocolError("Dev 입력과 outcome의 실행 메타데이터가 다릅니다.")
    predictions = [
        hash_regex.predict_episode(episode, artifact) for episode in inputs.episodes
    ]
    predicted_scores = [item[0] for item in predictions]
    predicted_costs = [item[1] for item in predictions]
    calibrated: Dict[str, float] = {}
    reports: Dict[str, Any] = {}
    for tier in TIERS:
        best = None
        for safety in _safety_candidates(policy, tier, grid_size):
            selected, predicted_ratio = hash_regex.select_models(
                predicted_scores,
                predicted_costs,
                budget_multiplier=float(policy.tiers[tier].budget_multiplier),
                safety_ratio=safety,
            )
            report = _score_one_tier(inputs, outcomes, policy, tier, selected)
            if not report["budget_passed"]:
                continue
            rank = (
                Decimal(report["tier_score"]),
                -Decimal(report["budget_ratio"]),
                -Decimal(str(safety)),
            )
            if best is None or rank > best[0]:
                best = (rank, safety, predicted_ratio, report)
        if best is None:
            raise RuntimeError(f"{tier} Dev 예산을 통과하는 안전계수가 없습니다.")
        calibrated[tier] = best[1]
        reports[tier] = {
            "safety_ratio": best[1],
            "predicted_budget_ratio": best[2],
            "actual_budget_ratio": best[3]["budget_ratio"],
            "tier_score": best[3]["tier_score"],
            "budget_passed": best[3]["budget_passed"],
        }
    return calibrated, reports


def _head_dict(intercept: float, coefficients: Any) -> Mapping[str, Any]:
    return {
        "intercept": float(intercept),
        "coefficients": [float(value) for value in coefficients],
    }


def _artifact_dict(
    *,
    hash_bins: int,
    policy: RoutingPolicy,
    mean: Any,
    scale: Any,
    intercept: Any,
    coefficients: Any,
    safety_ratios: Mapping[str, float],
    training_summary: Mapping[str, Any],
) -> Mapping[str, Any]:
    model_count = len(MODEL_IDS)
    return {
        "artifact_type": hash_regex.ARTIFACT_TYPE,
        "schema_version": 1,
        "feature_version": hash_regex.FEATURE_VERSION,
        "hash_algorithm": "fnv1a64-signed-word-1-2",
        "hash_bins": hash_bins,
        "dense_feature_names": list(hash_regex.DENSE_FEATURE_NAMES),
        "model_ids": list(MODEL_IDS),
        "policy_id": policy.policy_id,
        "policy_sha256": policy_sha256(policy),
        "feature_mean": [float(value) for value in mean],
        "feature_scale": [float(value) for value in scale],
        "score_heads": {
            model_id: _head_dict(intercept[index], coefficients[:, index])
            for index, model_id in enumerate(MODEL_IDS)
        },
        "log_cost_heads": {
            model_id: _head_dict(
                intercept[model_count + index],
                coefficients[:, model_count + index],
            )
            for index, model_id in enumerate(MODEL_IDS)
        },
        "tier_safety_ratios": {
            tier: float(safety_ratios[tier]) for tier in TIERS
        },
        "training_summary": dict(training_summary),
    }


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        temporary.write_text(
            json.dumps(
                value,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.chmod(0o644)
        os.replace(str(temporary), str(path))
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def train(
    *,
    input_path: Path,
    outcomes_path: Path,
    artifact_path: Path,
    report_path: Path,
    policy: RoutingPolicy,
    hash_bins: int,
    requested_folds: int,
    alpha_candidates: Sequence[float],
    safety_grid_size: int,
    validation_input_path: Optional[Path] = None,
    validation_outcomes_path: Optional[Path] = None,
) -> Mapping[str, Any]:
    _require_numpy()
    inputs = load_input(input_path)
    outcomes = load_outcomes(outcomes_path)
    if inputs.schema_version != policy.schema_version:
        raise ProtocolError("Train 입력과 정책의 schema_version이 다릅니다.")
    if len(inputs.episodes) < 2:
        raise ValueError("학습에는 문항이 두 개 이상 필요합니다.")
    if requested_folds < 2:
        raise ValueError("OOF fold 수는 2 이상이어야 합니다.")
    if safety_grid_size < 2:
        raise ValueError("안전계수 grid 크기는 2 이상이어야 합니다.")
    if (validation_input_path is None) != (validation_outcomes_path is None):
        raise ValueError("Dev 입력과 outcome 경로는 함께 지정해야 합니다.")
    if not alpha_candidates or any(
        not math.isfinite(alpha) or alpha <= 0 for alpha in alpha_candidates
    ):
        raise ValueError("ridge alpha 후보는 0보다 큰 유한한 수여야 합니다.")
    folds = min(requested_folds, len(inputs.episodes))
    matrix, targets = _training_matrix(inputs, outcomes, policy, hash_bins)
    alpha, oof_predictions, alpha_diagnostics = _select_alpha(
        matrix,
        targets,
        folds=folds,
        candidates=alpha_candidates,
    )
    safety_ratios, oof_reports = _select_safety_ratios(
        inputs,
        outcomes,
        policy,
        oof_predictions,
        safety_grid_size,
    )
    mean, scale, intercept, coefficients = _fit_ridge(matrix, targets, alpha)
    fitted_predictions = _predict_ridge(
        matrix,
        mean,
        scale,
        intercept,
        coefficients,
    )
    safety_ratios, fitted_safety_reports = _validate_fitted_safety_ratios(
        inputs,
        outcomes,
        policy,
        fitted_predictions,
        safety_ratios,
        safety_grid_size,
    )
    training_summary = {
        "num_episodes": len(inputs.episodes),
        "folds": folds,
        "ridge_alpha": alpha,
        "hash_bins": hash_bins,
        "input_sha256": _file_sha256(input_path),
        "outcomes_sha256": _file_sha256(outcomes_path),
        "optimizer": "numpy-ridge-oof-grid-v1",
    }
    initial_artifact_value = _artifact_dict(
        hash_bins=hash_bins,
        policy=policy,
        mean=mean,
        scale=scale,
        intercept=intercept,
        coefficients=coefficients,
        safety_ratios=safety_ratios,
        training_summary=training_summary,
    )
    validation_reports = None
    if validation_input_path is not None and validation_outcomes_path is not None:
        validation_inputs = load_input(validation_input_path)
        validation_outcomes = load_outcomes(validation_outcomes_path)
        initial_artifact = hash_regex.parse_artifact(initial_artifact_value)
        safety_ratios, validation_reports = _calibrate_validation_safety_ratios(
            validation_inputs,
            validation_outcomes,
            policy,
            initial_artifact,
            safety_grid_size,
        )
        training_summary.update(
            {
                "validation_num_episodes": len(validation_inputs.episodes),
                "validation_input_sha256": _file_sha256(validation_input_path),
                "validation_outcomes_sha256": _file_sha256(
                    validation_outcomes_path
                ),
            }
        )
    artifact_value = _artifact_dict(
        hash_bins=hash_bins,
        policy=policy,
        mean=mean,
        scale=scale,
        intercept=intercept,
        coefficients=coefficients,
        safety_ratios=safety_ratios,
        training_summary=training_summary,
    )
    artifact = hash_regex.parse_artifact(artifact_value)
    submissions = [
        hash_regex.make_hash_regex_submission(inputs, policy, artifact, tier).submission
        for tier in TIERS
    ]
    fitted_report = score_submissions(inputs, outcomes, submissions, policy)
    report = {
        "report_type": "ossp-hash-regex-training-v1",
        "policy_id": policy.policy_id,
        "policy_sha256": policy_sha256(policy),
        "training_summary": training_summary,
        "feature_dimension": matrix.shape[1],
        "alpha_objectives": alpha_diagnostics,
        "oof_tier_selection": oof_reports,
        "fitted_safety_validation": fitted_safety_reports,
        "fitted_train_self_check": fitted_report,
    }
    if validation_reports is not None:
        report["validation_safety_calibration"] = validation_reports
    _write_json_atomic(artifact_path, artifact_value)
    _write_json_atomic(report_path, report)
    return report


def _positive_float_list(value: str) -> Tuple[float, ...]:
    try:
        result = tuple(float(item) for item in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("alpha 목록을 해석할 수 없습니다.") from exc
    if not result or any(not math.isfinite(item) or item <= 0 for item in result):
        raise argparse.ArgumentTypeError("alpha는 0보다 큰 유한한 수여야 합니다.")
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="공개 Train으로 regex·feature-hashing 선형 라우터를 학습합니다."
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--outcomes", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--validation-input", type=Path)
    parser.add_argument("--validation-outcomes", type=Path)
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--hash-bins", type=int, default=hash_regex.DEFAULT_HASH_BINS)
    parser.add_argument("--folds", type=int, default=5)
    parser.add_argument(
        "--alphas",
        type=_positive_float_list,
        default=_positive_float_list("0.1,1,10,100"),
    )
    parser.add_argument("--safety-grid-size", type=int, default=121)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        policy = (
            load_policy(args.policy)
            if args.policy is not None
            else load_bundled_policy()
        )
        report = train(
            input_path=args.input,
            outcomes_path=args.outcomes,
            artifact_path=args.artifact,
            report_path=args.report,
            policy=policy,
            hash_bins=args.hash_bins,
            requested_folds=args.folds,
            alpha_candidates=args.alphas,
            safety_grid_size=args.safety_grid_size,
            validation_input_path=args.validation_input,
            validation_outcomes_path=args.validation_outcomes,
        )
    except (OSError, ProtocolError, RuntimeError, ValueError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2
    print(
        "OK: hash-regex artifact를 생성했습니다 "
        f"(Train self-check {report['fitted_train_self_check']['final_score']})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
