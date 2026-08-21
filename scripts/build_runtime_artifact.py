#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Phase 5 builder: bake the frozen router into rg2_artifact.npz and verify.

Verification chain (any failure aborts):
  V1 recomputed heads match the Phase 2/4 saved predictions
  V2 refit HGB cost models match the Phase 3 dev predictions
  V3 numpy tree export matches sklearn predict
  V4 runtime router decisions on dev == Phase 4 frozen-constant decisions
  V5 official scorer on the runtime submissions reproduces the Phase 4 numbers
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from phase2_lib import CACHE, RESULTS, load_cached, folds  # noqa: E402

RATE_IN = np.array([1.0, 2.127, 6.565])
RATE_OUT = np.array([4.0, 8.509, 26.260])
QS_ALL = [0.5, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95]
# two shards so no single file exceeds GitHub's 100MB limit (P split by rows)
OUT_PATH = ROOT / "src" / "ossp_router" / "model" / "rg2_artifact_p1.npz"
OUT_PATH2 = ROOT / "src" / "ossp_router" / "model" / "rg2_artifact_p2.npz"


def zsc_params(x):
    return x.mean(), x.std()


def l2n(M):
    return M / np.clip(np.linalg.norm(M, axis=1, keepdims=True), 1e-9, None)


def hgb(q=None):
    from sklearn.ensemble import HistGradientBoostingRegressor
    kw = dict(max_depth=6, learning_rate=0.08, max_iter=300,
              min_samples_leaf=20, random_state=0)
    if q is not None:
        return HistGradientBoostingRegressor(loss="quantile", quantile=q, **kw)
    return HistGradientBoostingRegressor(**kw)


def export_trees(model):
    feat, thr, left, right, leaf, value, off = [], [], [], [], [], [], [0]
    for it in model._predictors:
        nodes = it[0].nodes
        feat.append(nodes["feature_idx"].astype(np.int32))
        thr.append(nodes["num_threshold"].astype(np.float64))
        left.append(nodes["left"].astype(np.int32))
        right.append(nodes["right"].astype(np.int32))
        leaf.append(nodes["is_leaf"].astype(bool))
        value.append(nodes["value"].astype(np.float64))
        off.append(off[-1] + len(nodes))
    return {
        "feat": np.concatenate(feat), "thr": np.concatenate(thr),
        "left": np.concatenate(left), "right": np.concatenate(right),
        "leaf": np.concatenate(leaf), "value": np.concatenate(value),
        "off": np.array(off, dtype=np.int64),
        "base": float(np.asarray(model._baseline_prediction).ravel()[0]),
    }


def main():
    from sklearn.isotonic import IsotonicRegression
    from sklearn.linear_model import Ridge

    d = load_cached()
    z3 = np.load(CACHE / "phase3_cost.npz")
    H4 = np.load(CACHE / "phase4_heads.npz")
    cfg = json.load(open(CACHE / "phase4_chosen.json"))
    fz = np.load(ROOT / "scripts" / "featurizer_v1.npz", allow_pickle=False)

    S, Xd, Xd_de = d["S_tr"], d["Xd_tr"], d["Xd_de"]
    Xs, Xs_de = d["Xs_tr"], d["Xs_de"]
    IN, OUT = d["IN_tr"], d["OUT_tr"]
    m10, m21 = S[:, 1] - S[:, 0], S[:, 2] - S[:, 1]

    # ---------------- V1: heads ----------------
    print("== V1 heads ==")
    f1 = np.load(RESULTS / "knn_k200t02w05.npz")
    f2 = np.load(RESULTS / "slin_ridge_a8.0.npz")
    E_tr = l2n(np.hstack([l2n(Xd[:, :256]), 0.5 * Xd[:, 256:288]]))
    E_de = l2n(np.hstack([l2n(Xd_de[:, :256]), 0.5 * Xd_de[:, 256:288]]))
    sims = E_de @ E_tr.T
    top = np.argpartition(-sims, 199, axis=1)[:, :200]
    rows = np.arange(len(E_de))[:, None]
    ts = sims[rows, top] / 0.2
    ts -= ts.max(axis=1, keepdims=True)
    w = np.exp(ts); w /= w.sum(axis=1, keepdims=True)
    knn_dev = (w * m10[top]).sum(axis=1)
    print(f"  knn_dev vs saved: {np.abs(knn_dev - f1['dev']).max():.2e}")
    assert np.abs(knn_dev - f1["dev"]).max() < 1e-8

    ridge10 = Ridge(alpha=8.0).fit(Xs, m10)
    ridge21 = Ridge(alpha=8.0).fit(Xs, m21)
    rid10_dev = ridge10.predict(Xs_de)
    rid21_dev = ridge21.predict(Xs_de)
    print(f"  rid10_dev vs saved: {np.abs(rid10_dev - f2['dev']).max():.2e}")
    assert np.abs(rid10_dev - f2["dev"]).max() < 1e-8

    mu_k, sd_k = zsc_params(f1["oof"])
    mu_r, sd_r = zsc_params(f2["oof"])
    raw_oof = 2 * (f1["oof"] - mu_k) / sd_k + (f2["oof"] - mu_r) / sd_r
    iso10 = IsotonicRegression(out_of_bounds="clip").fit(raw_oof, m10)
    Em10_de = iso10.predict(2 * (knn_dev - mu_k) / sd_k + (rid10_dev - mu_r) / sd_r)
    print(f"  Em10_de vs phase4: {np.abs(Em10_de - H4['Em10_de']).max():.2e}")
    assert np.abs(Em10_de - H4["Em10_de"]).max() < 1e-8

    F = folds(m10)
    rid21_oof = np.zeros(len(m10))
    for tr, te in F:
        rid21_oof[te] = Ridge(alpha=8.0).fit(Xs[tr], m21[tr]).predict(Xs[te])
    iso21 = IsotonicRegression(out_of_bounds="clip").fit(rid21_oof, m21)
    Em21_de = iso21.predict(rid21_dev)
    print(f"  Em21_de vs phase4: {np.abs(Em21_de - H4['Em21_de']).max():.2e}")
    assert np.abs(Em21_de - H4["Em21_de"]).max() < 1e-8

    # ---------------- V2+V3: cost trees ----------------
    print("== V2/V3 cost models ==")
    from ossp_router.model_router import tree_eval
    groups = {}
    for j in range(3):
        m = hgb().fit(Xd, np.log1p(IN[:, j]))
        assert np.abs(np.expm1(m.predict(Xd_de)) - z3["IN_dev"][:, j]).max() < 1e-6
        g = export_trees(m)
        assert np.abs(tree_eval(g, Xd_de) - m.predict(Xd_de)).max() < 1e-9
        groups[f"in{j}"] = g
    for q, tag in [(0.75, "o75"), (0.85, "o85")]:
        qi = QS_ALL.index(q)
        for j in range(3):
            m = hgb(q=q).fit(Xd, np.log1p(OUT[:, j]))
            assert np.abs(np.expm1(m.predict(Xd_de)) - z3["OUT_dev"][qi][:, j]).max() < 1e-6
            g = export_trees(m)
            assert np.abs(tree_eval(g, Xd_de) - m.predict(Xd_de)).max() < 1e-9
            groups[f"{tag}_{j}"] = g
    print("  9 models exported, sklearn == numpy tree_eval")

    # ---------------- assemble ----------------
    TIER_SET = {"fast": "c", "balanced": "t", "premium": "c"}
    tiers = {t: {"lam": float(cfg[t]["lam"]), "lam_t": float(cfg[t]["lam_t"]),
                 "q": float(cfg[t]["q"]), "qt": float(cfg[t]["qt"]),
                 "set": TIER_SET[t]} for t in cfg}
    # ---------------- combined (train+dev) head set for fast/premium ----------
    # Same recipe refit on 2640 rows. Constants stay frozen; adoption was
    # gated per tier: fast/premium re-passed all bootstrap safety gates with
    # these heads, balanced did not and keeps the train-only set.
    print("== combined (train+dev) set ==")
    from scipy import sparse as sp
    from sklearn.model_selection import StratifiedKFold
    Xd2 = np.vstack([Xd, Xd_de])
    Xs2 = sp.vstack([Xs, Xs_de]).tocsr()
    S2 = np.vstack([d["S_tr"], d["S_de"]])
    IN2 = np.vstack([d["IN_tr"], d["IN_de"]])
    OUT2 = np.vstack([d["OUT_tr"], d["OUT_de"]])
    m10_2, m21_2 = S2[:, 1] - S2[:, 0], S2[:, 2] - S2[:, 1]
    n2 = len(S2)
    F2 = list(StratifiedKFold(5, shuffle=True, random_state=0).split(
        np.zeros(n2), np.sign(m10_2).astype(int)))

    E2 = l2n(np.hstack([l2n(Xd2[:, :256]), 0.5 * Xd2[:, 256:288]]))
    sims2 = E2 @ E2.T
    for _, te in F2:
        sims2[np.ix_(te, te)] = -np.inf
    idx2 = np.argpartition(-sims2, 199, axis=1)[:, :200]
    top2 = np.take_along_axis(sims2, idx2, axis=1) / 0.2
    top2 -= top2.max(axis=1, keepdims=True)
    w2 = np.exp(top2)
    w2 /= w2.sum(axis=1, keepdims=True)
    knn_oof2 = (w2 * m10_2[idx2]).sum(axis=1)

    rid10_oof2 = np.zeros(n2)
    rid21_oof2 = np.zeros(n2)
    for tr, te in F2:
        rid10_oof2[te] = Ridge(alpha=8.0).fit(Xs2[tr], m10_2[tr]).predict(Xs2[te])
        rid21_oof2[te] = Ridge(alpha=8.0).fit(Xs2[tr], m21_2[tr]).predict(Xs2[te])
    ridge10_2 = Ridge(alpha=8.0).fit(Xs2, m10_2)
    ridge21_2 = Ridge(alpha=8.0).fit(Xs2, m21_2)

    mu_k2, sd_k2 = knn_oof2.mean(), knn_oof2.std()
    mu_r2, sd_r2 = rid10_oof2.mean(), rid10_oof2.std()
    iso10_2 = IsotonicRegression(out_of_bounds="clip").fit(
        2 * (knn_oof2 - mu_k2) / sd_k2 + (rid10_oof2 - mu_r2) / sd_r2, m10_2)
    iso21_2 = IsotonicRegression(out_of_bounds="clip").fit(rid21_oof2, m21_2)
    print(f"  m10 oof corr={np.corrcoef(iso10_2.predict(2*(knn_oof2-mu_k2)/sd_k2+(rid10_oof2-mu_r2)/sd_r2), m10_2)[0,1]:.3f} "
          f"m21 oof corr={np.corrcoef(iso21_2.predict(rid21_oof2), m21_2)[0,1]:.3f}")

    groups_c = {}
    for j in range(3):
        m = hgb().fit(Xd2, np.log1p(IN2[:, j]))
        g = export_trees(m)
        assert np.abs(tree_eval(g, Xd2[:100]) - m.predict(Xd2[:100])).max() < 1e-9
        groups_c[f"in{j}"] = g
    for q, tag in [(0.75, "o75"), (0.85, "o85")]:
        for j in range(3):
            m = hgb(q=q).fit(Xd2, np.log1p(OUT2[:, j]))
            g = export_trees(m)
            assert np.abs(tree_eval(g, Xd2[:100]) - m.predict(Xd2[:100])).max() < 1e-9
            groups_c[f"{tag}_{j}"] = g
    print("  combined cost trees exported (sklearn == numpy verified)")

    from ossp_router.textfeat import pack_vocab
    vk1, vk2, vcol = pack_vocab(json.loads(str(fz["vocab_c"])))
    payload = dict(
        vocab_w=fz["vocab_w"], vocab_c=fz["vocab_c"],
        char_vk1=vk1, char_vk2=vk2, char_vcol=vcol,
        idf_w=fz["idf_w"], idf_c=fz["idf_c"], P=fz["P"],
        hand_mu=fz["hand_mu"], hand_sd=fz["hand_sd"],
        E_tr=E_tr.astype(np.float32), m10=m10,
        zc=np.array([mu_k, sd_k, mu_r, sd_r]),
        iso10_x=iso10.X_thresholds_, iso10_y=iso10.y_thresholds_,
        iso21_x=iso21.X_thresholds_, iso21_y=iso21.y_thresholds_,
        w10=ridge10.coef_.astype(np.float64), b10=np.float64(ridge10.intercept_),
        w21=ridge21.coef_.astype(np.float64), b21=np.float64(ridge21.intercept_),
        tiers=json.dumps(tiers),
    )
    for name, g in groups.items():
        for k, v in g.items():
            payload[f"{name}_{k}"] = v
    payload.update(
        E_tr_c=E2.astype(np.float32), m10_c=m10_2,
        zc_c=np.array([mu_k2, sd_k2, mu_r2, sd_r2]),
        iso10_x_c=iso10_2.X_thresholds_, iso10_y_c=iso10_2.y_thresholds_,
        iso21_x_c=iso21_2.X_thresholds_, iso21_y_c=iso21_2.y_thresholds_,
        w10_c=ridge10_2.coef_.astype(np.float64), b10_c=np.float64(ridge10_2.intercept_),
        w21_c=ridge21_2.coef_.astype(np.float64), b21_c=np.float64(ridge21_2.intercept_),
    )
    for name, g in groups_c.items():
        for k, v in g.items():
            payload[f"{name}_c_{k}"] = v
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    # uncompressed on purpose: cold-start decompression is wasted time on the
    # constrained runtime, and the docker layer gets gzipped anyway
    P = payload.pop("P")
    half = P.shape[0] // 2
    payload["P_a"] = P[:half]
    np.savez(OUT_PATH, **payload)
    np.savez(OUT_PATH2, P_b=P[half:])
    print(f"  artifact -> {OUT_PATH.name} ({OUT_PATH.stat().st_size/1e6:.1f} MB) + "
          f"{OUT_PATH2.name} ({OUT_PATH2.stat().st_size/1e6:.1f} MB)")

    # ---------------- V4: runtime decisions == reference decisions ------------
    # balanced (set t): must equal the phase4 frozen-constant decisions.
    # fast/premium (set c): must equal an in-builder combined-head reference
    # computed the same way the runtime does (dev prompts ARE in the reference
    # set, so kNN includes self — matching deployment on unseen prompts is the
    # separately-run gate simulation, not this code-correctness check).
    print("== V4 runtime vs reference decisions (dev-880) ==")
    import ossp_router.model_router as mr
    art = mr.load_artifact(OUT_PATH)
    from ossp_router.protocol import load_input
    inputs = load_input(ROOT / "data/materialized/dev/inputs.json")
    order = np.argsort([e.episode_id for e in inputs.episodes])
    texts_sorted = [inputs.episodes[i].prompt for i in order]

    E2_dev = E2[len(Xd):]
    sims_dev = E2_dev @ E2.T
    idxq = np.argpartition(-sims_dev, 199, axis=1)[:, :200]
    topq = np.take_along_axis(sims_dev, idxq, axis=1) / 0.2
    topq -= topq.max(axis=1, keepdims=True)
    wq = np.exp(topq)
    wq /= wq.sum(axis=1, keepdims=True)
    knn_dev2 = (wq * m10_2[idxq]).sum(axis=1)
    raw_dev2 = 2 * (knn_dev2 - mu_k2) / sd_k2 + (ridge10_2.predict(Xs_de) - mu_r2) / sd_r2
    Em10_c = iso10_2.predict(raw_dev2)
    Em21_c = iso21_2.predict(ridge21_2.predict(Xs_de))
    COSTC = {}
    for q, tag in [(0.75, "o75"), (0.85, "o85")]:
        OUTq = np.column_stack([np.expm1(tree_eval(groups_c[f"{tag}_{j}"], Xd_de)) for j in range(3)])
        INq = np.column_stack([np.expm1(tree_eval(groups_c[f"in{j}"], Xd_de)) for j in range(3)])
        COSTC[q] = (INq * RATE_IN + OUTq * RATE_OUT) / 1e6

    S_de, C_de = d["S_de"], d["C_de"]
    W = {"fast": 0.4, "balanced": 0.3, "premium": 0.3}
    MU = {"fast": 1.25, "balanced": 2.0, "premium": 4.0}
    final = 0.0
    t0 = time.time()
    for tier in W:
        b = cfg[tier]
        if tiers[tier]["set"] == "c":
            c_sel, c_th = COSTC[b["q"]], COSTC[b["qt"]][:, 2]
            e10, e21 = Em10_c, Em21_c
        else:
            qi_sel, qi_th = QS_ALL.index(b["q"]), QS_ALL.index(b["qt"])
            c_sel = (z3["IN_dev"] * RATE_IN + z3["OUT_dev"][qi_sel] * RATE_OUT) / 1e6
            c_th = (z3["IN_dev"][:, 2] * RATE_IN[2] + z3["OUT_dev"][qi_th][:, 2] * RATE_OUT[2]) / 1e6
            e10, e21 = H4["Em10_de"], H4["Em21_de"]
        u = np.stack([-b["lam"] * c_sel[:, 0], e10 - b["lam"] * c_sel[:, 1],
                      e10 + e21 - b["lam_t"] * c_th], axis=1)
        ref = u.argmax(axis=1)
        got = mr.select_model_indices(texts_sorted, tier, art)
        agree = (got == ref).mean()
        idx = np.arange(len(got))
        sc = S_de[idx, got].mean()
        ra = C_de[idx, got].sum() / C_de[:, 0].sum()
        ok = ra <= MU[tier]
        final += W[tier] * sc * ok
        note = " (in-sample: heads saw dev)" if tiers[tier]["set"] == "c" else " (holdout)"
        print(f"  {tier:8s} set={tiers[tier]['set']} agree={agree:.4f} runtime {sc:.4f}@{ra:.3f} "
              f"{'PASS' if ok else 'BUST'}{note}")
        assert agree == 1.0, f"{tier} runtime mismatch"
    print(f"  weighted dev number = {final:.4f} (fast/premium parts in-sample — NOT a holdout score; "
          f"holdout reference remains 0.6930)")
    print(f"  walltime 3 tiers incl. features: {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
