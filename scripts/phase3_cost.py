#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Phase 3: per-model cost models.

- input tokens: mean regression (near-deterministic given the prompt)
- output tokens: quantile regression on log1p(out_tokens), per model x quantile
- quantify: OOF coverage, underestimation tail, adverse selection under the
  per-item selection rule, mean-vs-quantile bust demo
- persist OOF/dev predictions for Phase 4 lambda calibration
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from phase2_lib import CACHE, RESULTS, load_cached, folds  # noqa: E402

QS = [0.5, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95]
RATE_IN = np.array([1.0, 2.127, 6.565])
RATE_OUT = np.array([4.0, 8.509, 26.260])
MODELS = ["light", "ax31", "think"]


def hgb(loss="squared_error", q=None):
    from sklearn.ensemble import HistGradientBoostingRegressor
    kw = dict(max_depth=6, learning_rate=0.08, max_iter=300,
              min_samples_leaf=20, random_state=0)
    if q is not None:
        return HistGradientBoostingRegressor(loss="quantile", quantile=q, **kw)
    return HistGradientBoostingRegressor(loss=loss, **kw)


def main():
    d = load_cached()
    X, Xde = d["Xd_tr"], d["Xd_de"]
    IN, INde, OUT, OUTde = d["IN_tr"], d["IN_de"], d["OUT_tr"], d["OUT_de"]
    S, C, Sde, Cde = d["S_tr"], d["C_tr"], d["S_de"], d["C_de"]
    m_tr = S[:, 1] - S[:, 0]
    F = folds(m_tr)
    n, nde = len(X), len(Xde)

    # ---------------- input tokens ----------------
    print("== input tokens (mean regression, log1p target) ==")
    IN_oof = np.zeros((n, 3))
    IN_dev = np.zeros((nde, 3))
    for j in range(3):
        y = np.log1p(IN[:, j])
        for tr, te in F:
            IN_oof[te, j] = np.expm1(hgb().fit(X[tr], y[tr]).predict(X[te]))
        IN_dev[:, j] = np.expm1(hgb().fit(X, y).predict(Xde))
        cc = np.corrcoef(IN_oof[:, j], IN[:, j])[0, 1]
        mape = np.median(np.abs(IN_oof[:, j] - IN[:, j]) / IN[:, j])
        print(f"  {MODELS[j]:6s} OOF corr={cc:.4f} medAPE={mape:.3f}")

    # ---------------- output tokens: quantile grid ----------------
    print("\n== output tokens quantile regression: OOF coverage (target=q) ==")
    OUT_oof = np.zeros((len(QS), n, 3))
    OUT_dev = np.zeros((len(QS), nde, 3))
    for qi, q in enumerate(QS):
        line = f"  q={q:.2f} "
        for j in range(3):
            y = np.log1p(OUT[:, j])
            for tr, te in F:
                OUT_oof[qi, te, j] = np.expm1(hgb(q=q).fit(X[tr], y[tr]).predict(X[te]))
            OUT_dev[qi, :, j] = np.expm1(hgb(q=q).fit(X, y).predict(Xde))
            cov_tr = (OUT[:, j] <= OUT_oof[qi, :, j]).mean()
            cov_de = (OUTde[:, j] <= OUT_dev[qi, :, j]).mean()
            line += f"| {MODELS[j]} tr {cov_tr:.2f} de {cov_de:.2f} "
        print(line)

    # mean regression for comparison (the naive baseline)
    OUT_mean_oof = np.zeros((n, 3))
    OUT_mean_dev = np.zeros((nde, 3))
    for j in range(3):
        y = np.log1p(OUT[:, j])
        for tr, te in F:
            OUT_mean_oof[te, j] = np.expm1(hgb().fit(X[tr], y[tr]).predict(X[te]))
        OUT_mean_dev[:, j] = np.expm1(hgb().fit(X, y).predict(Xde))
        cc = np.corrcoef(np.log1p(OUT_mean_oof[:, j]), np.log1p(OUT[:, j]))[0, 1]
        print(f"  mean-reg {MODELS[j]:6s} OOF log-corr={cc:.3f} "
              f"(coverage as 'quantile': {(OUT[:, j] <= OUT_mean_oof[:, j]).mean():.2f})")

    # ---------------- underestimation tail ----------------
    print("\n== underestimation tail: actual/predicted out-tokens (train OOF) ==")
    for qi, q in [(2, 0.75), (4, 0.85), (6, 0.95)]:
        line = f"  q={q:.2f} "
        for j in range(3):
            r = OUT[:, j] / np.clip(OUT_oof[qi, :, j], 1, None)
            line += (f"| {MODELS[j]} p90={np.percentile(r,90):5.2f} "
                     f"p99={np.percentile(r,99):5.2f} max={r.max():6.1f} ")
        print(line)

    # ---------------- item-level cost & delta-cost quality ----------------
    def cost_pred(qi, IN_p, OUT_p):
        return (IN_p * RATE_IN + OUT_p[qi] * RATE_OUT) / 1e6

    print("\n== item-level predicted vs true cost (train OOF, q=0.75) ==")
    Chat = cost_pred(2, IN_oof, OUT_oof)
    for j in range(3):
        cc = np.corrcoef(np.log(Chat[:, j]), np.log(C[:, j]))[0, 1]
        print(f"  {MODELS[j]:6s} log-corr={cc:.3f}")
    dch, dct = Chat[:, 1] - Chat[:, 0], C[:, 1] - C[:, 0]
    print(f"  delta-cost (ax31-light) corr={np.corrcoef(dch, dct)[0,1]:.3f} "
          f"log-corr={np.corrcoef(np.log(np.clip(dch,1e-8,None)), np.log(np.clip(dct,1e-8,None)))[0,1]:.3f}")

    # ---------------- adverse selection ----------------
    print("\n== adverse selection: cheap-think-first subset (premium analog) ==")
    for qi, q in [(0, 0.5), (2, 0.75), (4, 0.85)]:
        sel = np.argsort(OUT_oof[qi, :, 2])[: int(0.3 * n)]  # 30% cheapest predicted think
        pred_spend = OUT_oof[qi, sel, 2].sum()
        real_spend = OUT[sel, 2].sum()
        cov_all = (OUT[:, 2] <= OUT_oof[qi, :, 2]).mean()
        cov_sel = (OUT[sel, 2] <= OUT_oof[qi, sel, 2]).mean()
        print(f"  q={q:.2f} realized/predicted spend={real_spend/pred_spend:5.2f} "
              f"coverage all={cov_all:.2f} -> selected={cov_sel:.2f}")

    print("\n== fast-tier rule with PREDICTED delta-cost (was true-cost in Phase 2) ==")
    from sklearn.isotonic import IsotonicRegression

    def zsc(x):
        return (x - x.mean()) / (x.std() + 1e-12)

    f1 = np.load(RESULTS / "knn_k200t02w05.npz")
    f2 = np.load(RESULTS / "slin_ridge_a8.0.npz")
    oof_h = 2 * zsc(f1["oof"]) + zsc(f2["oof"])
    dev_h = 2 * zsc(f1["dev"]) + zsc(f2["dev"])
    iso = IsotonicRegression(out_of_bounds="clip").fit(oof_h, m_tr)
    Em, Em_de = iso.predict(oof_h), iso.predict(dev_h)

    def sim(qi, lam_target=1.22):
        dch_tr = cost_pred(qi, IN_oof, OUT_oof)[:, 1] - cost_pred(qi, IN_oof, OUT_oof)[:, 0]
        dch_de = cost_pred(qi, IN_dev, OUT_dev)[:, 1] - cost_pred(qi, IN_dev, OUT_dev)[:, 0]
        dch_tr = np.clip(dch_tr, 1e-8, None)
        dch_de = np.clip(dch_de, 1e-8, None)
        lo, hi = 0.0, 1e9
        idx = np.arange(n)
        for _ in range(80):  # calibrate lam on train so REALIZED train ratio hits target
            lam = (lo + hi) / 2
            ch = (Em - lam * dch_tr > 0).astype(int)
            if C[idx, ch].sum() / C[:, 0].sum() > lam_target:
                lo = lam
            else:
                hi = lam
        ch = (Em - hi * dch_tr > 0).astype(int)
        tr_sc, tr_ra = S[idx, ch].mean(), C[idx, ch].sum() / C[:, 0].sum()
        chd = (Em_de - hi * dch_de > 0).astype(int)
        jdx = np.arange(nde)
        de_sc, de_ra = Sde[jdx, chd].mean(), Cde[jdx, chd].sum() / Cde[:, 0].sum()
        return hi, tr_sc, tr_ra, de_sc, de_ra

    for qi, q in [(0, 0.5), (2, 0.75), (4, 0.85)]:
        lam, ts, tr, ds, dr = sim(qi)
        print(f"  q={q:.2f} lam={lam:7.1f} train {ts:.4f}@{tr:.3f} dev {ds:.4f}@{dr:.3f} "
              f"{'BUST' if dr > 1.25 else 'ok  '}")

    np.savez_compressed(
        CACHE / "phase3_cost.npz",
        QS=np.array(QS),
        IN_oof=IN_oof, IN_dev=IN_dev,
        OUT_oof=OUT_oof, OUT_dev=OUT_dev,
        OUT_mean_oof=OUT_mean_oof, OUT_mean_dev=OUT_mean_dev,
        Em=Em, Em_de=Em_de,
    )
    print("\nsaved ->", CACHE / "phase3_cost.npz")


if __name__ == "__main__":
    main()
