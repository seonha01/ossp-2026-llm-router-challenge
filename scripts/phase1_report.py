#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Phase 1 report: featurizer dims, timing, determinism, quick signal sanity."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from analyze import load_split  # noqa: E402
from features import DOMAINS, Featurizer, domain_rule, hand_feature_names  # noqa: E402


def load_prompts(split: str):
    mat = ROOT / "data" / "materialized" / split / "inputs.json"
    path = mat if mat.exists() else ROOT / "data" / split / "inputs-base.json"
    data = json.load(open(path))
    eps = sorted(data["episodes"], key=lambda e: e["episode_id"])
    return [e["episode_id"] for e in eps], [e["prompt"] for e in eps]


def main():
    tr_ids, tr_texts = load_prompts("train")
    de_ids, de_texts = load_prompts("dev")
    print(f"prompts: train {len(tr_texts)}, dev {len(de_texts)}")
    lens = np.array([len(t) for t in tr_texts + de_texts])
    print(f"prompt chars: median {np.median(lens):.0f}, p95 {np.percentile(lens, 95):.0f}, max {lens.max()}")

    t0 = time.time()
    feat = Featurizer()
    feat.fit(tr_texts)
    t_fit = time.time() - t0
    print(f"\nfit on train: {t_fit:.1f}s | word vocab {len(feat.wv.vocab)}, char vocab {len(feat.cv.vocab)}")

    t0 = time.time()
    Xd = feat.dense(de_texts)
    t_dense = time.time() - t0
    print(f"dense(dev-868): {t_dense:.1f}s -> shape {Xd.shape}  (runtime budget 90s/tier)")

    t0 = time.time()
    Xs = feat.sparse(de_texts)
    print(f"sparse(dev-868): {time.time()-t0:.1f}s -> shape {Xs.shape}, nnz/row {Xs.nnz/Xs.shape[0]:.0f}")

    # determinism: shuffled input order must give identical per-prompt features
    rng = np.random.RandomState(7)
    perm = rng.permutation(len(de_texts))
    Xd_perm = feat.dense([de_texts[i] for i in perm])
    same = np.array_equal(Xd_perm, Xd[perm])
    print(f"determinism (order shuffle, bitwise): {'PASS' if same else 'FAIL'}")

    art = ROOT / "scripts" / "featurizer_v1.npz"
    feat.save(str(art))
    print(f"artifact: {art.name} {art.stat().st_size/1e6:.1f} MB")
    f2 = Featurizer.load(str(art))
    same2 = np.allclose(f2.dense(de_texts[:50]), Xd[:50], atol=1e-5)
    print(f"save/load round-trip: {'PASS' if same2 else 'FAIL'} (float32 P)")

    # domain distribution
    for name, texts in [("train", tr_texts), ("dev", de_texts)]:
        dom = domain_rule(texts)
        counts = {DOMAINS[k]: int((dom == k).sum()) for k in range(len(DOMAINS))}
        print(f"domain {name}: {counts}")

    # quick signal sanity: OOF AUC on train for the two decision margins
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import StratifiedKFold

    _, S, C, _, OUT = load_split("train")
    keep_ids, _ = load_prompts("train")
    full_ids = [e["episode_id"] for e in sorted(
        json.load(open(ROOT / "data/train/outcomes.json"))["episodes"],
        key=lambda x: x["episode_id"])]
    pos = {e: i for i, e in enumerate(full_ids)}
    sel = [pos[e] for e in keep_ids]
    S, OUT = S[sel], OUT[sel]

    Xtr_s = feat.sparse(tr_texts)
    Xtr_d = feat.dense(tr_texts)
    for label, y in [("ax31>light", (S[:, 1] > S[:, 0]).astype(int)),
                     ("think>ax31", (S[:, 2] > S[:, 1]).astype(int)),
                     ("light<1.0", (S[:, 0] < 1.0).astype(int))]:
        aucs_s, aucs_d = [], []
        for tr_i, te_i in StratifiedKFold(5, shuffle=True, random_state=0).split(Xtr_d, y):
            m = LogisticRegression(max_iter=2000, C=1.0).fit(Xtr_s[tr_i], y[tr_i])
            aucs_s.append(roc_auc_score(y[te_i], m.predict_proba(Xtr_s[te_i])[:, 1]))
            m = LogisticRegression(max_iter=5000, C=1.0).fit(Xtr_d[tr_i], y[tr_i])
            aucs_d.append(roc_auc_score(y[te_i], m.predict_proba(Xtr_d[te_i])[:, 1]))
        print(f"OOF AUC {label:11s} base {y.mean():.2f} | sparse {np.mean(aucs_s):.3f} | dense {np.mean(aucs_d):.3f}")

    # cost signal sanity: log-output-token regression corr (think)
    from sklearn.linear_model import Ridge
    y = np.log1p(OUT[:, 2])
    preds = np.zeros(len(y))
    from sklearn.model_selection import KFold
    for tr_i, te_i in KFold(5, shuffle=True, random_state=0).split(Xtr_d):
        preds[te_i] = Ridge(alpha=2.0).fit(Xtr_d[tr_i], y[tr_i]).predict(Xtr_d[te_i])
    print(f"OOF corr log(out_tok think): {np.corrcoef(preds, y)[0,1]:.3f}")


if __name__ == "__main__":
    main()
