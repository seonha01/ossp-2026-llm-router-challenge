#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Component ablation study.

Every variant removes or swaps exactly one component, then goes through the
SAME calibration (grid + bootstrap safety gates, train only) and one dev
evaluation. So the numbers answer "what does this component buy in the final
score, holding the procedure fixed" rather than comparing raw predictors.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from phase2_lib import CACHE, RESULTS, load_cached, folds  # noqa: E402
from phase4_lambda import (  # noqa: E402
    Boot, CAP_GRID, GATES, QS_ALL, QT_GRID, Q_GRID, R_GRID, RATE_IN, RATE_OUT,
    TIER_MULT, TIER_W, U_GRID, build_insample_costs, decide, hgb, knn_pred,
    l2n, realized, zsc,
)

OUT_JSON = CACHE / "ablation_results.json"


def calibrate_and_eval(name, H, cost_of, d, boot, use_gates=True, r_grid=None):
    """H: dict Em10/Em21 (+_de/_in). cost_of(tag, split)->(n,3). Returns row."""
    S, C, S_de, C_de = d["S_tr"], d["C_tr"], d["S_de"], d["C_de"]
    q_grid = cost_of("qgrid", None)
    qt_grid = cost_of("qtgrid", None)
    tok95_tr = cost_of("tok95", "tr")
    tok95_in = cost_of("tok95", "in")
    tok95_de = cost_of("tok95", "de")

    chosen = {}
    for tier, mu in TIER_MULT.items():
        rows = []
        u_grid = list(U_GRID)
        while True:
            for q in q_grid:
                c_tr = cost_of(q, "tr")
                for qt in qt_grid:
                    ct_tr = cost_of(qt, "tr")[:, 2]
                    for cap in CAP_GRID:
                        veto = tok95_tr > cap
                        for r in (r_grid or R_GRID)[tier]:
                            lo, hi = 0.0, 1e12
                            for _ in range(60):
                                lt = (lo + hi) / 2
                                if (decide(H["Em10"], H["Em21"], c_tr, ct_tr, veto, 0.0, lt) == 2).mean() > r:
                                    lo = lt
                                else:
                                    hi = lt
                            lam_t = hi
                            for u in u_grid:
                                if any(abs(x["u"] - u) < 1e-9 and x["q"] == q and x["qt"] == qt
                                       and x["cap"] == cap and x["r"] == r for x in rows):
                                    continue
                                lo, hi = 0.0, 1e12
                                for _ in range(60):
                                    lam = (lo + hi) / 2
                                    ch = decide(H["Em10"], H["Em21"], c_tr, ct_tr, veto, lam, max(lam_t, lam))
                                    if realized(ch, S, C)[1] > u * mu:
                                        lo = lam
                                    else:
                                        hi = lam
                                lam = hi
                                lam_t_f = max(lam_t, lam)
                                ch = decide(H["Em10"], H["Em21"], c_tr, ct_tr, veto, lam, lam_t_f)
                                sc, ra, tf = realized(ch, S, C)
                                g = dict(
                                    iid=boot.bust(ch, C, mu, "iid"),
                                    shift=boot.bust(ch, C, mu, "shift"),
                                    small=boot.bust(ch, C, mu, "small"),
                                )
                                ch_in = decide(H["Em10_in"], H["Em21_in"], cost_of(q, "in"),
                                               cost_of(qt, "in")[:, 2], tok95_in > cap, lam, lam_t_f)
                                g["refit"] = boot.bust(ch_in, C, mu, "iid")
                                rows.append(dict(q=q, qt=qt, cap=float(cap), r=r, u=u,
                                                 lam=lam, lam_t=lam_t_f, sc=sc, ra=ra, tf=tf, **g))
            safe = [x for x in rows if all(x[k] <= v for k, v in GATES.items())]
            if not use_gates:
                safe = rows  # ablation: ignore the gates entirely
            if safe or min(u_grid) <= 0.70:
                break
            u_grid = [min(u_grid) - 0.04]
        pool = safe if safe else rows
        chosen[tier] = max(pool, key=lambda x: x["sc"])

    final = 0.0
    tiers_out = {}
    for tier, mu in TIER_MULT.items():
        b = chosen[tier]
        veto_de = tok95_de > b["cap"]
        ch_de = decide(H["Em10_de"], H["Em21_de"], cost_of(b["q"], "de"),
                       cost_of(b["qt"], "de")[:, 2], veto_de, b["lam"], b["lam_t"])
        sc, ra, tf = realized(ch_de, S_de, C_de)
        passed = bool(ra <= mu)
        final += TIER_W[tier] * (sc if passed else 0.0)
        tiers_out[tier] = dict(score=round(float(sc), 4), ratio=round(float(ra), 3),
                               passed=passed, think=round(float(tf), 3))
    row = dict(name=name, final=round(float(final), 4), tiers=tiers_out)
    print(json.dumps(row, ensure_ascii=False), flush=True)
    return row


def cost_provider(z, extra_mean=False, true_C=None, d=None):
    """Standard cost arrays from phase3; tags: quantiles + optional 'mean'."""
    def cost_of(tag, split):
        if tag == "qgrid":
            return ["mean"] if extra_mean else [0.5, 0.75, 0.85]
        if tag == "qtgrid":
            return ["mean"] if extra_mean else [0.85, 0.95]
        if tag == "tok95":
            src = {"tr": z["OUT_oof"][QS_ALL.index(0.95)],
                   "de": z["OUT_dev"][QS_ALL.index(0.95)],
                   "in": z["OUT_in95"]}[split]
            return src[:, 2]
        if true_C is not None:
            return true_C[split]
        if tag == "mean":
            OUT = {"tr": z["OUT_mean_oof"], "de": z["OUT_mean_dev"], "in": z["OUT_mean_in"]}[split]
        else:
            qi = QS_ALL.index(tag)
            OUT = {"tr": z["OUT_oof"][qi], "de": z["OUT_dev"][qi], "in": z["OUT_in"][tag]}[split]
        IN = {"tr": z["IN_oof"], "de": z["IN_dev"], "in": z["IN_in"]}[split]
        return (IN * RATE_IN + OUT * RATE_OUT) / 1e6

    return cost_of


def main():
    d = load_cached()
    z3 = dict(np.load(CACHE / "phase3_cost.npz"))
    S, Xd, Xd_de = d["S_tr"], d["Xd_tr"], d["Xd_de"]
    Xs, Xs_de = d["Xs_tr"], d["Xs_de"]
    m10, m21 = S[:, 1] - S[:, 0], S[:, 2] - S[:, 1]
    F = folds(m10)
    boot = Boot(len(S), d["dom_tr"])

    # in-sample cost refits (shared by the G4 gate in every variant)
    IN_in, OUT_in_all = build_insample_costs(d)
    z3["IN_in"] = IN_in
    z3["OUT_in"] = {q: OUT_in_all[q] for q in (0.5, 0.75, 0.85, 0.95)}
    z3["OUT_in95"] = OUT_in_all[0.95]
    from sklearn.ensemble import HistGradientBoostingRegressor
    OUT = d["OUT_tr"]
    z3["OUT_mean_in"] = np.column_stack([
        np.expm1(hgb().fit(Xd, np.log1p(OUT[:, j])).predict(Xd)) for j in range(3)])

    # ---- reference heads (identical to phase4) ----
    from phase4_lambda import build_heads
    H_full = build_heads(d)
    from sklearn.isotonic import IsotonicRegression
    from sklearn.linear_model import Ridge

    f1 = np.load(RESULTS / "knn_k200t02w05.npz")
    f2 = np.load(RESULTS / "slin_ridge_a8.0.npz")
    mu_k, sd_k = f1["oof"].mean(), f1["oof"].std()
    mu_r, sd_r = f2["oof"].mean(), f2["oof"].std()

    def iso_head(raw_oof, raw_dev, raw_in, target):
        iso = IsotonicRegression(out_of_bounds="clip").fit(raw_oof, target)
        return iso.predict(raw_oof), iso.predict(raw_dev), iso.predict(raw_in)

    knn10_in = knn_pred(l2n(np.hstack([l2n(Xd[:, :256]), 0.5 * Xd[:, 256:288]])),
                        l2n(np.hstack([l2n(Xd[:, :256]), 0.5 * Xd[:, 256:288]])),
                        m10, exclude_self=True)
    rid10_in = Ridge(alpha=8.0).fit(Xs, m10).predict(Xs)

    cost_std = cost_provider(z3)
    results = []

    def head_pack(Em):
        return dict(Em10=Em[0][0], Em10_de=Em[0][1], Em10_in=Em[0][2],
                    Em21=Em[1][0], Em21_de=Em[1][1], Em21_in=Em[1][2])

    # A. full reference (must reproduce 0.6930)
    results.append(calibrate_and_eval("full", H_full, cost_std, d, boot))

    # B. no quality heads: constant train-mean margins
    const = lambda v, n: np.full(n, v)
    n, nde = len(S), len(d["S_de"])
    H_B = dict(Em10=const(m10.mean(), n), Em10_de=const(m10.mean(), nde), Em10_in=const(m10.mean(), n),
               Em21=const(m21.mean(), n), Em21_de=const(m21.mean(), nde), Em21_in=const(m21.mean(), n))
    results.append(calibrate_and_eval("no_quality", H_B, cost_std, d, boot))

    # C/D. single-head Em10 (Em21 unchanged)
    for nm, raws in [("knn_only", (f1["oof"], f1["dev"], knn10_in)),
                     ("ridge_only", (f2["oof"], f2["dev"], rid10_in))]:
        Em10 = iso_head(*raws, m10)
        H_v = dict(H_full)
        H_v["Em10"], H_v["Em10_de"], H_v["Em10_in"] = Em10
        results.append(calibrate_and_eval(nm, H_v, cost_std, d, boot))

    # E. no isotonic calibration: raw blends
    raw10 = (2 * zsc(f1["oof"]) + zsc(f2["oof"]),
             2 * (f1["dev"] - mu_k) / sd_k + (f2["dev"] - mu_r) / sd_r,
             2 * (knn10_in - mu_k) / sd_k + (rid10_in - mu_r) / sd_r)
    rid21_oof = np.zeros(n)
    for tr, te in F:
        rid21_oof[te] = Ridge(alpha=8.0).fit(Xs[tr], m21[tr]).predict(Xs[te])
    r21_full = Ridge(alpha=8.0).fit(Xs, m21)
    H_E = dict(Em10=raw10[0], Em10_de=raw10[1], Em10_in=raw10[2],
               Em21=rid21_oof, Em21_de=r21_full.predict(Xs_de), Em21_in=r21_full.predict(Xs))
    results.append(calibrate_and_eval("no_isotonic", H_E, cost_std, d, boot))

    # F. no think model
    H_F = dict(H_full)
    for k in ("Em21", "Em21_de", "Em21_in"):
        H_F[k] = np.full(len(H_full[k]), -1e9)
    results.append(calibrate_and_eval("no_think", H_F, cost_std, d, boot))

    # G. mean cost instead of upper quantiles
    cost_mean = cost_provider(z3, extra_mean=True)

    results.append(calibrate_and_eval("mean_cost", H_full, cost_mean, d, boot))

    # H. true costs (oracle cost model, reference upper bound)
    true_C = {"tr": d["C_tr"], "de": d["C_de"], "in": d["C_tr"]}
    results.append(calibrate_and_eval(
        "true_cost", H_full,
        lambda tag, split: (
            [0.75] if tag in ("qgrid", "qtgrid")
            else (cost_std("tok95", split) if tag == "tok95" else true_C[split])),
        d, boot))

    # I. no safety gates (pick the best-scoring config regardless)
    results.append(calibrate_and_eval("no_gates", H_full, cost_std, d, boot, use_gates=False))

    # J. no hand/domain features (SVD-256 only) for kNN and cost trees
    E0 = l2n(Xd[:, :256])
    E0_de = l2n(Xd_de[:, :256])
    knn_oof_J = np.zeros(n)
    sims = E0 @ E0.T
    for _, te in F:
        sims[np.ix_(te, te)] = -np.inf
    idx = np.argpartition(-sims, 199, axis=1)[:, :200]
    top = np.take_along_axis(sims, idx, axis=1) / 0.2
    top -= top.max(axis=1, keepdims=True)
    w = np.exp(top); w /= w.sum(axis=1, keepdims=True)
    knn_oof_J = (w * m10[idx]).sum(axis=1)
    knn_dev_J = knn_pred(E0_de, E0, m10)
    knn_in_J = knn_pred(E0, E0, m10, exclude_self=True)
    muJ, sdJ = knn_oof_J.mean(), knn_oof_J.std()
    rawJ = (2 * zsc(knn_oof_J) + zsc(f2["oof"]),
            2 * (knn_dev_J - muJ) / sdJ + (f2["dev"] - mu_r) / sd_r,
            2 * (knn_in_J - muJ) / sdJ + (rid10_in - mu_r) / sd_r)
    Em10_J = iso_head(*rawJ, m10)
    H_J = dict(H_full)
    H_J["Em10"], H_J["Em10_de"], H_J["Em10_in"] = Em10_J
    XdJ, XdJ_de = Xd[:, :256], Xd_de[:, :256]
    zJ = dict(z3)
    for j in range(3):
        y = np.log1p(d["IN_tr"][:, j])
        preds = np.zeros(n)
        for tr, te in F:
            preds[te] = np.expm1(hgb().fit(XdJ[tr], y[tr]).predict(XdJ[te]))
        zJ.setdefault("IN_oof_J", np.zeros((n, 3)))[:, j] = preds
        m_full = hgb().fit(XdJ, y)
        zJ.setdefault("IN_dev_J", np.zeros((nde, 3)))[:, j] = np.expm1(m_full.predict(XdJ_de))
        zJ.setdefault("IN_in_J", np.zeros((n, 3)))[:, j] = np.expm1(m_full.predict(XdJ))
    for q in (0.75, 0.85, 0.95):
        oo = np.zeros((n, 3)); dd = np.zeros((nde, 3)); ii = np.zeros((n, 3))
        for j in range(3):
            y = np.log1p(OUT[:, j])
            for tr, te in F:
                oo[te, j] = np.expm1(hgb(q=q).fit(XdJ[tr], y[tr]).predict(XdJ[te]))
            m_full = hgb(q=q).fit(XdJ, y)
            dd[:, j] = np.expm1(m_full.predict(XdJ_de))
            ii[:, j] = np.expm1(m_full.predict(XdJ))
        zJ[f"J_{q}"] = {"tr": oo, "de": dd, "in": ii}

    def cost_J(tag, split):
        if tag == "qgrid":
            return [0.75, 0.85]
        if tag == "qtgrid":
            return [0.85, 0.95]
        if tag == "tok95":
            return zJ["J_0.95"][split][:, 2]
        OUTx = zJ[f"J_{tag}"][split]
        INx = {"tr": zJ["IN_oof_J"], "de": zJ["IN_dev_J"], "in": zJ["IN_in_J"]}[split]
        return (INx * RATE_IN + OUTx * RATE_OUT) / 1e6

    results.append(calibrate_and_eval("no_hand_features", H_J, cost_J, d, boot))

    json.dump(results, open(OUT_JSON, "w"), ensure_ascii=False, indent=1)
    print("\nsaved ->", OUT_JSON)


if __name__ == "__main__":
    main()
