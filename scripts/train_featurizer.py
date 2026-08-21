#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Fit the text featurizer on the full materialized train split."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from features import Featurizer  # noqa: E402


def main():
    inputs = ROOT / "data/materialized/train/inputs.json"
    if not inputs.exists():
        raise SystemExit(
            "data/materialized/train/inputs.json이 없습니다. 먼저 "
            ".venv-data/bin/python tools/materialize_public_data.py 를 실행하십시오."
        )
    data = json.load(open(inputs))
    episodes = sorted(data["episodes"], key=lambda e: e["episode_id"])
    texts = [e["prompt"] for e in episodes]
    print(f"fitting featurizer on {len(texts)} train prompts ...")
    t0 = time.time()
    feat = Featurizer()
    feat.fit(texts)
    out = ROOT / "scripts" / "featurizer_v1.npz"
    feat.save(str(out))
    print(f"saved {out} ({out.stat().st_size/1e6:.1f} MB) in {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
