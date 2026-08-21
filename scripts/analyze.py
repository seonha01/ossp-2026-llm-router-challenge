#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Phase 0 harness.

1) Reproduce the background numbers (fixed policies, oracles, ties, think tail)
   on the FULL outcome sets (train 1760 / dev 880, including AIME-only episodes).
2) Provide an offline evaluator: (choice indices, tier) -> (mean score, cost
   ratio, budget pass) and cross-check it against src/ossp_router/scoring.py
   on the 868-episode dev subset that has redistributable prompts.
3) Quick demo: naive TF-IDF+Ridge mean-cost router -> does it bust the budget?
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

MODELS = ["ax31-light", "ax31", "axk1-think"]
RATE_IN = np.array([1.0, 2.127, 6.565])
RATE_OUT = np.array([4.0, 8.509, 26.260])
TIER_MULT = {"fast": 1.25, "balanced": 2.0, "premium": 4.0}
TIER_W = {"fast": 0.4, "balanced": 0.3, "premium": 0.3}


def load_split(split: str, filtered: bool = False):
    """Return (ids, S, C, IN, OUT) sorted by episode_id.

    S[i,m] true score, C[i,m] true cost in credits, IN/OUT token counts.
    """
    name = "outcomes_filtered.json" if filtered else "outcomes.json"
    data = json.load(open(ROOT / "data" / split / name))
    eps = sorted(data["episodes"], key=lambda e: e["episode_id"])
    ids = [e["episode_id"] for e in eps]
    S = np.array([[float(e["models"][m]["score"]) for m in MODELS] for e in eps])
    IN = np.array([[e["models"][m]["input_tokens"] for m in MODELS] for e in eps], dtype=np.float64)
    OUT = np.array([[e["models"][m]["output_tokens"] for m in MODELS] for e in eps], dtype=np.float64)
    C = (IN * RATE_IN + OUT * RATE_OUT) / 1e6
    return ids, S, C, IN, OUT


def evaluate(choice: np.ndarray, S: np.ndarray, C: np.ndarray, tier: str):
    """Offline evaluator. Returns (tier score, raw mean score, cost ratio, passed)."""
    n = len(choice)
    idx = np.arange(n)
    light = C[:, 0].sum()
    cost = C[idx, choice].sum()
    raw = S[idx, choice].mean()
    ratio = cost / light
    passed = cost <= TIER_MULT[tier] * light
    return (raw if passed else 0.0), raw, ratio, passed


def oracle(S: np.ndarray, C: np.ndarray, mult: float, allowed=(0, 1, 2)):
    """Budget-constrained hindsight optimum via Lagrangian bisection + greedy refine."""
    Ss, Cs = S[:, list(allowed)], C[:, list(allowed)]
    n = len(Ss)
    idx = np.arange(n)
    limit = mult * C[:, 0].sum()
    lo, hi = 0.0, 1e9
    for _ in range(200):
        lam = (lo + hi) / 2.0
        ch = (Ss - lam * Cs).argmax(axis=1)
        if Cs[idx, ch].sum() > limit:
            lo = lam
        else:
            hi = lam
    ch = (Ss - hi * Cs).argmax(axis=1)
    # greedy refinement: spend leftover budget on the best remaining upgrades
    for _ in range(3):
        cur = Cs[idx, ch].sum()
        ups = []
        for j in range(Ss.shape[1]):
            ds = Ss[:, j] - Ss[idx, ch]
            dc = Cs[:, j] - Cs[idx, ch]
            for i in np.nonzero(ds > 1e-12)[0]:
                ups.append((ds[i] / max(dc[i], 1e-12), i, j, ds[i], dc[i]))
        ups.sort(reverse=True)
        changed = False
        for _, i, j, ds, dc in ups:
            if cur + dc <= limit + 1e-15 and Ss[i, j] > Ss[i, ch[i]]:
                cur += Cs[i, j] - Cs[i, ch[i]]
                ch[i] = j
                changed = True
        if not changed:
            break
    real = np.array(list(allowed))[ch]
    return S[idx, real].mean(), C[idx, real].sum() / C[:, 0].sum(), real


def crosscheck_scoring(ids, S, C):
    """Compare our evaluator with src/ossp_router/scoring.py on dev-868."""
    from ossp_router.protocol import (
        Decision, Submission, load_bundled_policy, load_input, load_outcomes,
    )
    from ossp_router.scoring import score_submissions

    inputs = load_input(ROOT / "data/dev/inputs-base.json")
    outcomes = load_outcomes(ROOT / "data/dev/outcomes_filtered.json")
    policy = load_bundled_policy()
    id_pos = {e: i for i, e in enumerate(ids)}

    rng_choice = {  # deterministic prompt-independent test policies
        "all-light": np.zeros(len(ids), dtype=int),
        "all-ax31": np.ones(len(ids), dtype=int),
        "mixed": np.array([i % 3 for i in range(len(ids))]),
    }
    print("\n== cross-check vs src/ossp_router/scoring.py (dev-868) ==")
    ok_all = True
    for name, ch in rng_choice.items():
        subs = []
        for tier in TIER_MULT:
            decisions = tuple(
                Decision(ep.episode_id, MODELS[int(ch[id_pos[ep.episode_id]])])
                for ep in inputs.episodes
            )
            subs.append(Submission(
                schema_version=inputs.schema_version,
                challenge_id=inputs.challenge_id,
                policy_id=policy.policy_id,
                split=inputs.split,
                tier=tier,
                decisions=decisions,
            ))
        report = score_submissions(inputs, outcomes, subs, policy)
        for tier in TIER_MULT:
            official_score = float(report["tiers"][tier]["tier_score"])
            official_ratio = float(report["tiers"][tier]["budget_ratio"])
            official_pass = report["tiers"][tier]["budget_passed"]
            ours_score, _, ours_ratio, ours_pass = evaluate(ch, S, C, tier)
            match = (abs(official_score - ours_score) < 1e-9
                     and abs(official_ratio - ours_ratio) < 1e-9
                     and official_pass == ours_pass)
            ok_all &= match
            print(f"  {name:9s} {tier:8s} official={official_score:.6f}/{official_ratio:.4f}/{official_pass} "
                  f"ours={ours_score:.6f}/{ours_ratio:.4f}/{ours_pass}  {'OK' if match else 'MISMATCH'}")
    print("  =>", "ALL MATCH" if ok_all else "!!! MISMATCH FOUND !!!")
    return ok_all


def naive_ridge_demo():
    """Naive TF-IDF+Ridge mean-cost router: pick lambda on train, run on dev."""
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import Ridge

    tr_in = json.load(open(ROOT / "data/train/inputs-base.json"))
    de_in = json.load(open(ROOT / "data/dev/inputs-base.json"))
    tr_prompts = {e["episode_id"]: e["prompt"] for e in tr_in["episodes"]}
    de_prompts = {e["episode_id"]: e["prompt"] for e in de_in["episodes"]}

    tr_ids, tr_S, tr_C, _, _ = load_split("train")
    keep = [i for i, e in enumerate(tr_ids) if e in tr_prompts]
    tr_ids = [tr_ids[i] for i in keep]
    tr_S, tr_C = tr_S[keep], tr_C[keep]
    X_texts = [tr_prompts[e] for e in tr_ids]

    de_ids, de_S, de_C, _, _ = load_split("dev", filtered=True)
    Xd_texts = [de_prompts[e] for e in de_ids]

    vec = TfidfVectorizer(max_features=20000, ngram_range=(1, 2))
    Xtr = vec.fit_transform(X_texts)
    Xde = vec.transform(Xd_texts)

    S_hat = np.column_stack([Ridge(alpha=1.0).fit(Xtr, tr_S[:, m]).predict(Xde) for m in range(3)])
    C_hat = np.column_stack([Ridge(alpha=1.0).fit(Xtr, tr_C[:, m]).predict(Xde) for m in range(3)])
    C_hat_tr = np.column_stack([Ridge(alpha=1.0).fit(Xtr, tr_C[:, m]).predict(Xtr) for m in range(3)])
    S_hat_tr = np.column_stack([Ridge(alpha=1.0).fit(Xtr, tr_S[:, m]).predict(Xtr) for m in range(3)])
    C_hat = np.clip(C_hat, 1e-9, None)
    C_hat_tr = np.clip(C_hat_tr, 1e-9, None)

    print("\n== naive TF-IDF+Ridge (mean cost, lambda fixed on train, in-sample) ==")
    n_tr = len(tr_ids)
    idx_tr = np.arange(n_tr)
    for tier, mult in TIER_MULT.items():
        limit_tr = mult * C_hat_tr[:, 0].sum()  # naive: predicted light baseline
        lo, hi = 0.0, 1e9
        for _ in range(100):
            lam = (lo + hi) / 2.0
            ch = (S_hat_tr - lam * C_hat_tr).argmax(axis=1)
            if C_hat_tr[idx_tr, ch].sum() > limit_tr:
                lo = lam
            else:
                hi = lam
        lam = hi  # frozen constant
        ch_de = (S_hat - lam * C_hat).argmax(axis=1)
        tier_score, raw, ratio, passed = evaluate(ch_de, de_S, de_C, tier)
        print(f"  {tier:8s} lam={lam:9.1f} dev raw={raw:.4f} ratio={ratio:.3f} "
              f"limit={mult:.2f} passed={passed} -> tier score {tier_score:.4f}")


def main():
    for split, n_expect in [("train", 1760), ("dev", 880)]:
        ids, S, C, IN, OUT = load_split(split)
        assert len(ids) == n_expect, (split, len(ids))

    ids, S, C, IN, OUT = load_split("dev")
    n = len(ids)
    idx = np.arange(n)
    print(f"== dev full ({n} episodes, incl. AIME) ==")
    print("-- fixed policies --")
    for m, name in enumerate(MODELS):
        ch = np.full(n, m)
        _, raw, ratio, _ = evaluate(ch, S, C, "premium")
        print(f"  all-{name:11s} mean score={raw:.4f} cost ratio={ratio:.3f}")

    print("-- ties / win rates --")
    tie3 = np.mean((S[:, 0] == S[:, 1]) & (S[:, 1] == S[:, 2]))
    print(f"  3-way tie                  {tie3:.3f}")
    print(f"  ax31 > light               {np.mean(S[:, 1] > S[:, 0]):.3f}")
    print(f"  think > ax31               {np.mean(S[:, 2] > S[:, 1]):.3f}")
    print(f"  light is (joint) best      {np.mean(S[:, 0] >= S.max(axis=1)):.3f}")

    print("-- budget-constrained oracle (hindsight) --")
    final3, final2 = 0.0, 0.0
    rows = []
    for tier, mult in TIER_MULT.items():
        s3, r3, _ = oracle(S, C, mult)
        s2, r2, _ = oracle(S, C, mult, allowed=(0, 1))
        final3 += TIER_W[tier] * s3
        final2 += TIER_W[tier] * s2
        rows.append((tier, s3, r3, s2, r2))
        print(f"  {tier:8s} oracle={s3:.4f} (ratio {r3:.3f})   no-think oracle={s2:.4f} (ratio {r2:.3f})")
    all_light = S[:, 0].mean()
    print(f"  final oracle = {final3:.4f} | no-think final = {final2:.4f}"
          f" | gain share of light-vs-ax31 = {(final2 - all_light) / (final3 - all_light):.2f}")

    print("-- think cost tail (vs mean light cost) --")
    ref = C[:, 0].mean()
    rel = C[:, 2] / ref
    print(f"  median={np.median(rel):.1f}x p90={np.percentile(rel, 90):.1f}x "
          f"p99={np.percentile(rel, 99):.1f}x max={rel.max():.1f}x")

    ids868, S868, C868, _, _ = load_split("dev", filtered=True)
    print(f"\n== dev filtered ({len(ids868)} episodes, prompts available) ==")
    for m, name in enumerate(MODELS):
        ch = np.full(len(ids868), m)
        _, raw, ratio, _ = evaluate(ch, S868, C868, "premium")
        print(f"  all-{name:11s} mean score={raw:.4f} cost ratio={ratio:.3f}")
    f3 = sum(TIER_W[t] * oracle(S868, C868, m)[0] for t, m in TIER_MULT.items())
    print(f"  final oracle (868) = {f3:.4f}")

    ok = crosscheck_scoring(ids868, S868, C868)
    if not ok:
        sys.exit(1)

    naive_ridge_demo()


if __name__ == "__main__":
    main()
