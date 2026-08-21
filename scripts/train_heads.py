#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Train the two quality margin heads used by the deployed router.

Writes results/knn_k200t02w05.npz and results/slin_ridge_a8.0.npz
(train OOF predictions + dev predictions), the inputs the calibration and
artifact-build steps expect.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from phase2_lib import RESULTS, folds, load_cached  # noqa: E402


def l2n(M):
    norms = np.linalg.norm(M, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return M / norms


def main():
    from sklearn.linear_model import Ridge

    d = load_cached()
    Xd, Xd_de = d["Xd_tr"], d["Xd_de"]
    Xs, Xs_de = d["Xs_tr"], d["Xs_de"]
    m10 = d["S_tr"][:, 1] - d["S_tr"][:, 0]
    F = folds(m10)
    RESULTS.mkdir(parents=True, exist_ok=True)

    # --- similarity kNN head (k=200, temp=0.2, hand block weight 0.5) ---
    E_tr = l2n(np.hstack([l2n(Xd[:, :256]), 0.5 * Xd[:, 256:288]]))
    E_de = l2n(np.hstack([l2n(Xd_de[:, :256]), 0.5 * Xd_de[:, 256:288]]))
    sims = E_tr @ E_tr.T
    for _, te in F:  # OOF: block same-fold neighbours, self included
        sims[np.ix_(te, te)] = -np.inf

    def knn(sim_rows):
        idx = np.argpartition(-sim_rows, 199, axis=1)[:, :200]
        top = np.take_along_axis(sim_rows, idx, axis=1) / 0.2
        top -= top.max(axis=1, keepdims=True)
        w = np.exp(top)
        w /= w.sum(axis=1, keepdims=True)
        return (w * m10[idx]).sum(axis=1)

    knn_oof = knn(sims)
    knn_dev = knn(E_de @ E_tr.T)
    np.savez_compressed(RESULTS / "knn_k200t02w05.npz", oof=knn_oof, dev=knn_dev)

    # --- sparse ridge margin head (alpha=8) ---
    rid_oof = np.zeros(len(m10))
    for tr, te in F:
        rid_oof[te] = Ridge(alpha=8.0).fit(Xs[tr], m10[tr]).predict(Xs[te])
    rid_dev = Ridge(alpha=8.0).fit(Xs, m10).predict(Xs_de)
    np.savez_compressed(RESULTS / "slin_ridge_a8.0.npz", oof=rid_oof, dev=rid_dev)

    print(f"heads written to {RESULTS}")
    print(f"  knn corr(oof, m10) = {np.corrcoef(knn_oof, m10)[0, 1]:.3f}")
    print(f"  ridge corr(oof, m10) = {np.corrcoef(rid_oof, m10)[0, 1]:.3f}")


if __name__ == "__main__":
    main()
