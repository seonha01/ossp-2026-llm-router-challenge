#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Build the shared Phase 2 cache: outcome arrays + dense/sparse feature matrices."""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from features import Featurizer, domain_rule  # noqa: E402
from phase2_lib import CACHE, load_full  # noqa: E402


def main():
    CACHE.mkdir(parents=True, exist_ok=True)
    tr_ids, tr_texts, S_tr, C_tr, IN_tr, OUT_tr = load_full("train")
    de_ids, de_texts, S_de, C_de, IN_de, OUT_de = load_full("dev")

    feat = Featurizer.load(str(ROOT / "scripts" / "featurizer_v1.npz"))
    t0 = time.time()
    Xd_tr = feat.dense(tr_texts)
    Xd_de = feat.dense(de_texts)
    from scipy import sparse
    Xs_tr = feat.sparse(tr_texts)
    Xs_de = feat.sparse(de_texts)
    print(f"features in {time.time()-t0:.0f}s: dense {Xd_tr.shape}/{Xd_de.shape}, "
          f"sparse {Xs_tr.shape}/{Xs_de.shape}")

    np.savez_compressed(
        CACHE / "arrays.npz",
        S_tr=S_tr, C_tr=C_tr, IN_tr=IN_tr, OUT_tr=OUT_tr,
        S_de=S_de, C_de=C_de, IN_de=IN_de, OUT_de=OUT_de,
        Xd_tr=Xd_tr, Xd_de=Xd_de,
        dom_tr=domain_rule(tr_texts), dom_de=domain_rule(de_texts),
        ids_tr=np.array(tr_ids), ids_de=np.array(de_ids),
    )
    sparse.save_npz(CACHE / "Xs_tr.npz", Xs_tr.tocsr())
    sparse.save_npz(CACHE / "Xs_de.npz", Xs_de.tocsr())

    m_tr = S_tr[:, 1] - S_tr[:, 0]
    print(f"train margin: +{(m_tr>0).mean():.3f} / 0 {(m_tr==0).mean():.3f} / -{(m_tr<0).mean():.3f}")
    # context for the fast tier: cost of upgrading every positive-margin item
    up = np.where(m_tr > 0, 1, 0)
    idx = np.arange(len(up))
    print(f"upgrade-all-positives cost ratio (train): {C_tr[idx, up].sum()/C_tr[:,0].sum():.3f}")
    print("cache written to", CACHE)


if __name__ == "__main__":
    main()
