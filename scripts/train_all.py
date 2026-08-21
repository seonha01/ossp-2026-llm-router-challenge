#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""End-to-end training: raw public data -> deployed router artifact.

Runs the whole pipeline in order, each step a standalone script:

  1. train_featurizer.py  fit word/char TF-IDF + SVD + hand-feature scaler
  2. phase2_cache.py      cache feature matrices and outcome arrays
  3. train_heads.py       quality margin heads (kNN + sparse ridge)
  4. phase3_cost.py       input/output token models incl. quantile grid
  5. phase4_lambda.py     per-tier penalty constants via bootstrap safety gates
  6. build_runtime_artifact.py  export + verify src/ossp_router/model/rg2_artifact.npz

Everything is seeded, so a clean re-run reproduces the shipped artifact.
Cache location defaults to work/cache (override with OSSP_CACHE).
Prerequisite: tools/materialize_public_data.py has been run (AIME prompts).
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STEPS = [
    "train_featurizer.py",
    "phase2_cache.py",
    "train_heads.py",
    "phase3_cost.py",
    "phase4_lambda.py",
    "build_runtime_artifact.py",
]


def main():
    env = dict(os.environ)
    env.setdefault("OMP_NUM_THREADS", "16")
    total = time.time()
    for step in STEPS:
        print(f"\n===== {step} =====", flush=True)
        t0 = time.time()
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / step)], cwd=ROOT, env=env
        )
        if result.returncode != 0:
            raise SystemExit(f"{step} 실패 (exit {result.returncode})")
        print(f"===== {step} done in {time.time()-t0:.0f}s =====", flush=True)
    print(f"\nALL DONE in {(time.time()-total)/60:.1f} min")


if __name__ == "__main__":
    main()
