# SPDX-License-Identifier: Apache-2.0
"""Phase 2 shared harness: data, cached features, OOF protocol, operational metric.

Protocol (fixed for every experiment):
- Train = full materialized 1760, Dev = full materialized 880 (holdout, report-only).
- 5-fold OOF on train, StratifiedKFold(shuffle=True, random_state=0) on sign(margin).
- Selection of any threshold/hyperparameter happens on TRAIN OOF only.
- Operational metric = fast-tier no-think simulation:
    upgrade prompt to ax31 iff predicted margin > tau; tau chosen on train OOF to
    maximize realized train score subject to train cost ratio <= FILL_TO.
    Report dev score/ratio at that frozen tau (and dev-best tau as head ceiling).

Every experiment MUST call save_result() with its OOF and dev prediction arrays
so the stacking stage can combine heads.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
CACHE = Path(os.environ.get("OSSP_CACHE", ROOT / "work" / "cache"))
RESULTS = CACHE / "results"
MODELS = ["ax31-light", "ax31", "axk1-think"]
RATE_IN = np.array([1.0, 2.127, 6.565])
RATE_OUT = np.array([4.0, 8.509, 26.260])
FILL_TO = 1.22
N_FOLDS = 5
SEED = 0


def load_full(split: str):
    """ids, prompts, S, C, IN, OUT for the full materialized split, id-sorted."""
    inp = json.load(open(ROOT / "data" / "materialized" / split / "inputs.json"))
    prompts = {e["episode_id"]: e["prompt"] for e in inp["episodes"]}
    out = json.load(open(ROOT / "data" / split / "outcomes.json"))
    eps = sorted(out["episodes"], key=lambda e: e["episode_id"])
    ids = [e["episode_id"] for e in eps]
    S = np.array([[float(e["models"][m]["score"]) for m in MODELS] for e in eps])
    IN = np.array([[e["models"][m]["input_tokens"] for m in MODELS] for e in eps], dtype=np.float64)
    OUT = np.array([[e["models"][m]["output_tokens"] for m in MODELS] for e in eps], dtype=np.float64)
    C = (IN * RATE_IN + OUT * RATE_OUT) / 1e6
    texts = [prompts[i] for i in ids]
    return ids, texts, S, C, IN, OUT


def load_cached():
    """Cached matrices built by phase2_cache.py. Returns dict."""
    z = np.load(CACHE / "arrays.npz", allow_pickle=False)
    from scipy import sparse
    d = {k: z[k] for k in z.files}
    d["Xs_tr"] = sparse.load_npz(CACHE / "Xs_tr.npz")
    d["Xs_de"] = sparse.load_npz(CACHE / "Xs_de.npz")
    return d


def folds(margin: np.ndarray):
    from sklearn.model_selection import StratifiedKFold
    y = np.sign(margin).astype(int)
    skf = StratifiedKFold(N_FOLDS, shuffle=True, random_state=SEED)
    return list(skf.split(np.zeros(len(y)), y))


def fast_sim(margin_pred: np.ndarray, S: np.ndarray, C: np.ndarray, tau: float):
    """No-think fast-tier: upgrade to ax31 where pred > tau. Returns (score, ratio)."""
    ch = (margin_pred > tau).astype(int)
    idx = np.arange(len(ch))
    cost = C[idx, ch].sum()
    light = C[:, 0].sum()
    return S[idx, ch].mean(), cost / light


def pick_tau(margin_oof: np.ndarray, S: np.ndarray, C: np.ndarray):
    """Best tau on train OOF subject to ratio <= FILL_TO."""
    taus = np.unique(np.round(margin_oof, 4))
    best = (S[:, 0].mean(), 1.0, np.inf)  # score, ratio, tau  (upgrade nothing)
    grid = np.percentile(margin_oof, np.linspace(0, 100, 201))
    for tau in np.unique(np.concatenate([taus[:: max(1, len(taus) // 200)], grid])):
        sc, ra = fast_sim(margin_oof, S, C, tau)
        if ra <= FILL_TO and sc > best[0]:
            best = (sc, ra, float(tau))
    return best


def evaluate_head(name: str, oof: np.ndarray, dev: np.ndarray, notes: str = ""):
    """Standard report for a margin head. Saves JSON+NPZ, returns dict."""
    from sklearn.metrics import roc_auc_score

    d = load_cached()
    S_tr, C_tr, S_de, C_de = d["S_tr"], d["C_tr"], d["S_de"], d["C_de"]
    m_tr = S_tr[:, 1] - S_tr[:, 0]
    m_de = S_de[:, 1] - S_de[:, 0]

    tr_sc, tr_ra, tau = pick_tau(oof, S_tr, C_tr)
    de_sc, de_ra = fast_sim(dev, S_de, C_de, tau)
    de_best = max(
        (fast_sim(dev, S_de, C_de, t)[0], t)
        for t in np.percentile(dev, np.linspace(0, 100, 201))
        if fast_sim(dev, S_de, C_de, t)[1] <= FILL_TO
    )
    res = {
        "name": name,
        "oof_auc": float(roc_auc_score((m_tr > 0).astype(int), oof)),
        "oof_corr": float(np.corrcoef(oof, m_tr)[0, 1]),
        "train_fast": round(float(tr_sc), 4),
        "train_ratio": round(float(tr_ra), 3),
        "tau": tau,
        "dev_fast": round(float(de_sc), 4),
        "dev_ratio": round(float(de_ra), 3),
        "dev_fast_besttau": round(float(de_best[0]), 4),
        "dev_auc": float(roc_auc_score((m_de > 0).astype(int), dev)),
        "notes": notes,
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    json.dump(res, open(RESULTS / f"{name}.json", "w"), indent=1)
    np.savez_compressed(RESULTS / f"{name}.npz", oof=oof, dev=dev)
    return res
