#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Phase 4 v2: per-item 3-model rule + bootstrap-calibrated lambdas + think caps.

Rule (constants frozen at train time, per-item, order-independent):
    u_light = - lam * chat_light(x; q)
    u_ax31  = Em10(x) - lam * chat_ax31(x; q)
    u_think = Em10(x) + Em21(x) - lam_t * chat_think(x; q_think)
              (vetoed if predicted p95 think out-tokens > cap_tok)
    choice  = argmax

Safety gates, all on TRAIN with realized outcomes (dev never touched):
    G1 P(ratio > mu) <= 1%   bootstrap iid, n=880
    G2 P(ratio > mu) <= 2%   bootstrap code->40% mix, n=880
    G3 P(ratio > mu) <= 2%   bootstrap iid, n=440 (small hidden set)
    G4 P(ratio > mu) <= 2%   decisions re-made with REFIT in-sample heads at the
                             frozen constants (catches OOF->refit drift), n=880
Among configs passing all gates, pick max train OOF score. If none pass,
extend the usage grid downward automatically.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from phase2_lib import CACHE, RESULTS, load_cached, folds  # noqa: E402

RATE_IN = np.array([1.0, 2.127, 6.565])
RATE_OUT = np.array([4.0, 8.509, 26.260])
TIER_MULT = {"fast": 1.25, "balanced": 2.0, "premium": 4.0}
TIER_W = {"fast": 0.4, "balanced": 0.3, "premium": 0.3}
R_GRID = {"fast": [0.0, 0.01, 0.02],
          "balanced": [0.0, 0.02, 0.04, 0.07],
          "premium": [0.08, 0.13, 0.18, 0.25]}
U_GRID = [0.82, 0.86, 0.90, 0.92, 0.94]
Q_GRID = [0.5, 0.75, 0.85]
QT_GRID = [0.85, 0.95]
CAP_GRID = [np.inf, 8000.0, 4000.0]
GATES = dict(iid=0.01, shift=0.02, small=0.02, refit=0.02)
NBOOT = 3000
SEED = 0
QS_ALL = [0.5, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95]


def zsc(x):
    return (x - x.mean()) / (x.std() + 1e-12)


def l2n(M):
    return M / np.clip(np.linalg.norm(M, axis=1, keepdims=True), 1e-9, None)


def hgb(q=None):
    from sklearn.ensemble import HistGradientBoostingRegressor
    kw = dict(max_depth=6, learning_rate=0.08, max_iter=300,
              min_samples_leaf=20, random_state=0)
    if q is not None:
        return HistGradientBoostingRegressor(loss="quantile", quantile=q, **kw)
    return HistGradientBoostingRegressor(**kw)


def knn_pred(Eq, Eref, mref, k=200, temp=0.2, exclude_self=False):
    sims = Eq @ Eref.T
    if exclude_self:
        np.fill_diagonal(sims, -9e9)
    kk = min(k, Eref.shape[0] - (1 if exclude_self else 0))
    top = np.argpartition(-sims, kk - 1, axis=1)[:, :kk]
    rows = np.arange(len(Eq))[:, None]
    w = np.exp(sims[rows, top] / temp)
    w /= w.sum(axis=1, keepdims=True)
    return (w * mref[top]).sum(axis=1)


def build_heads(d):
    """Em10/Em21: OOF (calibration), dev (final eval), refit in-sample (G4)."""
    from sklearn.isotonic import IsotonicRegression
    from sklearn.linear_model import Ridge

    S, Xd, Xd_de = d["S_tr"], d["Xd_tr"], d["Xd_de"]
    Xs, Xs_de = d["Xs_tr"], d["Xs_de"]
    m10, m21 = S[:, 1] - S[:, 0], S[:, 2] - S[:, 1]
    F = folds(m10)
    E_tr = l2n(np.hstack([l2n(Xd[:, :256]), 0.5 * Xd[:, 256:288]]))
    E_de = l2n(np.hstack([l2n(Xd_de[:, :256]), 0.5 * Xd_de[:, 256:288]]))

    out = {}
    # --- Em10: 2*knn + ridge blend (Phase 2 winner) ---
    f1, f2 = np.load(RESULTS / "knn_k200t02w05.npz"), np.load(RESULTS / "slin_ridge_a8.0.npz")
    raw_oof = 2 * zsc(f1["oof"]) + zsc(f2["oof"])
    mu_k, sd_k = f1["oof"].mean(), f1["oof"].std()
    mu_r, sd_r = f2["oof"].mean(), f2["oof"].std()
    raw_dev = 2 * (f1["dev"] - mu_k) / sd_k + (f2["dev"] - mu_r) / sd_r
    knn10_in = knn_pred(E_tr, E_tr, m10, exclude_self=True)
    rid10_in = Ridge(alpha=8.0).fit(Xs, m10).predict(Xs)
    raw_in = 2 * (knn10_in - mu_k) / sd_k + (rid10_in - mu_r) / sd_r
    iso = IsotonicRegression(out_of_bounds="clip").fit(raw_oof, m10)
    out["Em10"], out["Em10_de"], out["Em10_in"] = (
        iso.predict(raw_oof), iso.predict(raw_dev), iso.predict(raw_in))

    # --- Em21: ridge (best OOF corr in v1 check) ---
    rid_oof = np.zeros(len(S))
    for tr, te in F:
        rid_oof[te] = Ridge(alpha=8.0).fit(Xs[tr], m21[tr]).predict(Xs[te])
    rid_dev = Ridge(alpha=8.0).fit(Xs, m21).predict(Xs_de)
    rid_in = Ridge(alpha=8.0).fit(Xs, m21).predict(Xs)
    iso21 = IsotonicRegression(out_of_bounds="clip").fit(rid_oof, m21)
    out["Em21"], out["Em21_de"], out["Em21_in"] = (
        iso21.predict(rid_oof), iso21.predict(rid_dev), iso21.predict(rid_in))
    print(f"  Em10 corr oof={np.corrcoef(out['Em10'], m10)[0,1]:.3f}  "
          f"Em21 corr oof={np.corrcoef(out['Em21'], m21)[0,1]:.3f}")
    print(f"  refit drift: Em21 mean oof={out['Em21'].mean():.4f} in={out['Em21_in'].mean():.4f}")
    return out


def build_insample_costs(d):
    """Refit-on-train in-sample cost predictions for the G4 gate."""
    X, IN, OUT = d["Xd_tr"], d["IN_tr"], d["OUT_tr"]
    IN_in = np.column_stack([
        np.expm1(hgb().fit(X, np.log1p(IN[:, j])).predict(X)) for j in range(3)])
    OUT_in = {}
    for q in set(Q_GRID) | set(QT_GRID) | {0.95}:
        OUT_in[q] = np.column_stack([
            np.expm1(hgb(q=q).fit(X, np.log1p(OUT[:, j])).predict(X)) for j in range(3)])
    return IN_in, OUT_in


def decide(Em10, Em21, c_sel, c_think, veto, lam, lam_t):
    u = np.stack([
        -lam * c_sel[:, 0],
        Em10 - lam * c_sel[:, 1],
        np.where(veto, -9e9, Em10 + Em21 - lam_t * c_think),
    ], axis=1)
    return u.argmax(axis=1)


def realized(ch, S, C):
    idx = np.arange(len(ch))
    return S[idx, ch].mean(), C[idx, ch].sum() / C[:, 0].sum(), (ch == 2).mean()


class Boot:
    def __init__(self, n, dom, seed=SEED):
        rng = np.random.RandomState(seed)
        self.idx_iid = rng.randint(0, n, size=(NBOOT, 880))
        self.idx_small = rng.randint(0, n, size=(NBOOT, 440))
        w = np.where(dom == 0, 0.40 / max((dom == 0).mean(), 1e-9),
                     0.60 / max((dom != 0).mean(), 1e-9))
        p = w / w.sum()
        self.idx_shift = rng.choice(n, size=(NBOOT, 880), p=p)

    def bust(self, ch, C, mu, which):
        idx = getattr(self, f"idx_{which}")
        csel = C[np.arange(len(ch)), ch]
        return float((csel[idx].sum(1) > mu * C[:, 0][idx].sum(1)).mean())


def main():
    d = load_cached()
    z = np.load(CACHE / "phase3_cost.npz")
    S, C, S_de, C_de = d["S_tr"], d["C_tr"], d["S_de"], d["C_de"]
    n = len(S)
    print("== heads ==")
    H = build_heads(d)
    print("== in-sample cost refits (G4) ==")
    IN_in, OUT_in = build_insample_costs(d)
    boot = Boot(n, d["dom_tr"])

    def costs(q, split):
        qi = QS_ALL.index(q)
        if split == "tr":
            return (z["IN_oof"] * RATE_IN + z["OUT_oof"][qi] * RATE_OUT) / 1e6
        return (z["IN_dev"] * RATE_IN + z["OUT_dev"][qi] * RATE_OUT) / 1e6

    def costs_in(q):
        return (IN_in * RATE_IN + OUT_in[q] * RATE_OUT) / 1e6

    qi95 = QS_ALL.index(0.95)
    tok95_tr = z["OUT_oof"][qi95][:, 2]
    tok95_de = z["OUT_dev"][qi95][:, 2]
    tok95_in = OUT_in[0.95][:, 2]

    chosen = {}
    for tier, mu in TIER_MULT.items():
        rows = []
        u_grid = list(U_GRID)
        while True:
            for q in Q_GRID:
                c_tr = costs(q, "tr")
                for qt in QT_GRID:
                    ct_tr = costs(qt, "tr")[:, 2]
                    for cap in CAP_GRID:
                        veto = tok95_tr > cap
                        for r in R_GRID[tier]:
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
                                ch_in = decide(H["Em10_in"], H["Em21_in"], costs_in(q),
                                               costs_in(qt)[:, 2], tok95_in > cap, lam, lam_t_f)
                                g["refit"] = boot.bust(ch_in, C, mu, "iid")
                                ra_in = realized(ch_in, S, C)[1]
                                rows.append(dict(q=q, qt=qt, cap=float(cap), r=r, u=u,
                                                 lam=lam, lam_t=lam_t_f, sc=sc, ra=ra,
                                                 tf=tf, ra_in=ra_in, **g))
            safe = [x for x in rows if all(x[k] <= v for k, v in GATES.items())]
            if safe or min(u_grid) <= 0.70:
                break
            u_grid = [min(u_grid) - 0.04]
        best = max(safe, key=lambda x: x["sc"]) if safe else min(
            rows, key=lambda x: (x["iid"] + x["shift"] + x["small"] + x["refit"]))
        chosen[tier] = best
        print(f"\n== {tier} (mu={mu}) : {len(safe)}/{len(rows)} safe ==")
        for x in sorted(safe, key=lambda x: -x["sc"])[:4]:
            print(f"  q={x['q']:.2f}/{x['qt']:.2f} cap={x['cap']:.0f} think_tgt={x['r']:.2f} "
                  f"use={x['u']:.2f} -> {x['sc']:.4f}@{x['ra']:.3f} (in-sample ra {x['ra_in']:.3f}) "
                  f"think {x['tf']:.3f} G[{x['iid']:.3f}/{x['shift']:.3f}/{x['small']:.3f}/{x['refit']:.3f}]")
        b = best
        print(f"  CHOSEN q={b['q']}/{b['qt']} cap={b['cap']:.0f} r={b['r']} u={b['u']} "
              f"lam={b['lam']:.1f} lam_t={b['lam_t']:.1f} safe={bool(safe)}")

    print("\n== FROZEN CONSTANTS -> single dev evaluation ==")
    final = 0.0
    report = {}
    for tier, mu in TIER_MULT.items():
        b = chosen[tier]
        ch_de = decide(H["Em10_de"], H["Em21_de"], costs(b["q"], "de"),
                       costs(b["qt"], "de")[:, 2], tok95_de > b["cap"], b["lam"], b["lam_t"])
        sc, ra, tf = realized(ch_de, S_de, C_de)
        passed = ra <= mu
        final += TIER_W[tier] * (sc if passed else 0.0)
        cnt = np.bincount(ch_de, minlength=3)
        report[tier] = dict(dev_score=round(float(sc), 4), dev_ratio=round(float(ra), 3),
                            passed=bool(passed), think=round(float(tf), 3),
                            counts=cnt.tolist(), **{k: b[k] for k in
                            ("q", "qt", "cap", "r", "u", "lam", "lam_t", "sc", "ra")})
        print(f"  {tier:8s} dev {sc:.4f}@{ra:.3f} (mu {mu}) think {tf:.3f} "
              f"counts {cnt.tolist()} {'PASS' if passed else '** BUST **'}")
    print(f"  final_score = {final:.4f}")
    json.dump(report, open(CACHE / "phase4_chosen.json", "w"), indent=1)
    np.savez_compressed(CACHE / "phase4_heads.npz", **H)
    print("saved ->", CACHE / "phase4_chosen.json")


if __name__ == "__main__":
    main()
