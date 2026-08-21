# SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
# SPDX-License-Identifier: Apache-2.0

# RouteGuard v2: prompt-only, per-item router.
#
# For each prompt independently (no batch statistics, no episode metadata):
#   u_light = -lam * c_light(x)
#   u_ax31  = Em10(x) - lam * c_ax31(x)
#   u_think = Em10(x) + Em21(x) - lam_t * c_think(x)
#   pick argmax.
# Em10/Em21 are calibrated expected score margins (kNN + sparse ridge, isotonic).
# Costs decompose into near-exact input tokens and a conservative upper quantile
# of output tokens. All constants (lam, lam_t, quantile choice, calibration
# curves) are frozen at training time by bootstrap safety gates, so the same
# prompt always gets the same model regardless of batch order or composition.
#
# Runtime dependencies: stdlib + numpy only.

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional, Sequence

import numpy as np

from .protocol import (
    Decision,
    Episode,
    Message,
    ProtocolError,
    Submission,
    TIERS,
    load_bundled_policy,
    submission_to_dict,
    write_json,
)
from .textfeat import (
    CharPackedVectorizer,
    N_DOMAINS,
    WordPackedVectorizer,
    domain_rule_from_hand,
    hand_features,
    pack_vocab,
)

MODELS = ["ax31-light", "ax31", "axk1-think"]
RATE_IN = np.array([1.0, 2.127, 6.565])
RATE_OUT = np.array([4.0, 8.509, 26.260])
KNN_K = 200
KNN_TEMP = 0.2
HAND_W = 0.5

# The fitted arrays ship as two npz shards so no single file crosses GitHub's
# 100MB limit; the SVD projection P is split by rows and re-stacked at load.
_ARTIFACT_DIR = Path(__file__).with_name("model")
_ARTIFACT_PATHS = (_ARTIFACT_DIR / "rg2_artifact_p1.npz", _ARTIFACT_DIR / "rg2_artifact_p2.npz")
_ARTIFACT = None

# Two fitted head/cost sets share one featurizer. Set "t" is fitted on train
# only (its constants were validated with dev held out). Set "c" refits the
# same recipe on train+dev for extra data; it is used only by the tiers whose
# frozen constants re-passed every bootstrap safety gate under the refit
# (fast, premium). balanced failed the small-sample gate with refit heads and
# stays on set "t". Constants themselves are identical in both sets.
TREE_GROUPS = ["in0", "in1", "in2", "o75_0", "o75_1", "o75_2", "o85_0", "o85_1", "o85_2"]
HEAD_KEYS = ["E_tr", "m10", "zc", "iso10_x", "iso10_y", "iso21_x", "iso21_y",
             "w10", "b10", "w21", "b21"]


class _ShardedNpz:
    """Read-only view over the two artifact shards, P re-stacked lazily."""

    def __init__(self, p1: Path, p2: Path):
        self._z1 = np.load(p1, allow_pickle=False)
        self._z2 = np.load(p2, allow_pickle=False)
        self.files = list(self._z1.files) + ["P"]

    def __getitem__(self, key):
        if key == "P":
            return np.vstack([self._z1["P_a"], self._z2["P_b"]])
        return self._z1[key]

    def __contains__(self, key):
        return key in self.files


def load_artifact(path: Optional[Path] = None):
    global _ARTIFACT
    if _ARTIFACT is None or path is not None:
        if path is not None:
            p1 = Path(path)
            z = _ShardedNpz(p1, p1.with_name(p1.name.replace("_p1", "_p2")))
        else:
            z = _ShardedNpz(*_ARTIFACT_PATHS)
        art = {
            "vocab_w": json.loads(str(z["vocab_w"])),
            "vocab_c": json.loads(str(z["vocab_c"])),
            "idf_w": z["idf_w"].astype(np.float64),
            "idf_c": z["idf_c"].astype(np.float64),
            "P": z["P"].astype(np.float64),
            "hand_mu": z["hand_mu"], "hand_sd": z["hand_sd"],
            "tiers": json.loads(str(z["tiers"])),
        }
        art["sets"] = {}
        set_keys = ["t", "c"] if "E_tr_c" in z.files else ["t"]
        for sk in set_keys:
            sfx = "" if sk == "t" else "_c"
            hs = {
                # float32 on purpose: halves the cosine-similarity matmul cost
                # on the 2-core runtime; downstream math returns to float64
                "E_tr": z[f"E_tr{sfx}"].astype(np.float32),
                "m10": z[f"m10{sfx}"].astype(np.float64),
                "zc": z[f"zc{sfx}"].astype(np.float64),
                "iso10_x": z[f"iso10_x{sfx}"], "iso10_y": z[f"iso10_y{sfx}"],
                "iso21_x": z[f"iso21_x{sfx}"], "iso21_y": z[f"iso21_y{sfx}"],
                "w10": z[f"w10{sfx}"].astype(np.float64), "b10": float(z[f"b10{sfx}"]),
                "w21": z[f"w21{sfx}"].astype(np.float64), "b21": float(z[f"b21{sfx}"]),
            }
            for g in TREE_GROUPS:
                hs[g] = {
                    "feat": z[f"{g}{sfx}_feat"], "thr": z[f"{g}{sfx}_thr"],
                    "left": z[f"{g}{sfx}_left"], "right": z[f"{g}{sfx}_right"],
                    "leaf": z[f"{g}{sfx}_leaf"], "value": z[f"{g}{sfx}_value"],
                    "off": z[f"{g}{sfx}_off"], "base": float(z[f"{g}{sfx}_base"]),
                }
            art["sets"][sk] = hs
        art["bv_w"] = WordPackedVectorizer(art["vocab_w"], art["idf_w"], 0)
        if "char_vk1" in z:
            vk1, vk2, vcol = z["char_vk1"], z["char_vk2"], z["char_vcol"]
        else:
            vk1, vk2, vcol = pack_vocab(art["vocab_c"])
        art["bv_c"] = CharPackedVectorizer(vk1, vk2, vcol, art["idf_c"],
                                           len(art["vocab_w"]))
        _ARTIFACT = art
    return _ARTIFACT


def _episode_text(episode: Episode) -> str:
    if episode.prompt is not None:
        return episode.prompt
    assert episode.messages is not None
    return "\n".join(message.content for message in episode.messages)


class _FastInput:
    """Input batch parsed with the stock json module.

    The strict protocol parser re-validates every node with python hooks,
    which dominates cold-start on the constrained runtime. The router only
    needs the header fields and (episode_id, prompt/messages), so parse those
    directly; malformed files still fail with a clear error.
    """

    __slots__ = ("schema_version", "challenge_id", "split", "episodes")

    def __init__(self, path: Path):
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        try:
            self.schema_version = data["schema_version"]
            self.challenge_id = data["challenge_id"]
            self.split = data["split"]
            episodes = []
            for e in data["episodes"]:
                if "prompt" in e:
                    episodes.append(Episode(e["episode_id"], e["prompt"], None))
                else:
                    msgs = tuple(Message(m["role"], m["content"]) for m in e["messages"])
                    episodes.append(Episode(e["episode_id"], None, msgs))
            self.episodes = tuple(episodes)
        except (KeyError, TypeError) as exc:
            raise ProtocolError(f"입력 형식 오류: {exc}") from exc


def _l2n(M: np.ndarray) -> np.ndarray:
    return M / np.clip(np.linalg.norm(M, axis=1, keepdims=True), 1e-9, None)


def tree_eval(group, X: np.ndarray) -> np.ndarray:
    """Sum of exported HistGradientBoosting trees + baseline, exact replica.

    All trees of the ensemble descend together as an (n_samples, n_trees)
    node matrix, so the depth-6 walk costs a handful of large numpy ops.
    """
    feat, thr = group["feat"], group["thr"]
    left, right, leaf, value = group["left"], group["right"], group["leaf"], group["value"]
    off = group["off"]
    n = len(X)
    starts = off[:-1][None, :]
    node = np.broadcast_to(starts, (n, starts.shape[1])).copy()
    rows = np.arange(n)[:, None]
    while True:
        isleaf = leaf[node]
        if isleaf.all():
            break
        xv = X[rows, feat[node]]
        nxt = np.where(xv <= thr[node], left[node], right[node]) + starts
        node = np.where(isleaf, node, nxt)
    return group["base"] + value[node].sum(axis=1)


_FORK_ART = None


def _text_stage(texts: Sequence[str], art):
    """Per-prompt text work: tfidf projections, ridge dots, hand features."""
    n = len(texts)
    hs = art["sets"][art["_set"]]
    P = art["P"]
    Z = np.zeros((n, P.shape[1]))
    rid10 = np.full(n, hs["b10"])
    rid21 = np.full(n, hs["b21"])
    w10, w21 = hs["w10"], hs["w21"]
    for i, text in enumerate(texts):
        cw, vw = art["bv_w"].row(text)
        cc, vc = art["bv_c"].row(text)
        if len(cw):
            Z[i] += vw @ P[cw]
            rid10[i] += vw @ w10[cw]
            rid21[i] += vw @ w21[cw]
        if len(cc):
            Z[i] += vc @ P[cc]
            rid10[i] += vc @ w10[cc]
            rid21[i] += vc @ w21[cc]
    return Z, rid10, rid21, hand_features(texts)


def _predict_core(texts: Sequence[str], art):
    """Per-prompt predictions for one chunk: Em10, Em21, IN_hat, OUT75, OUT85."""
    n = len(texts)
    hs = art["sets"][art["_set"]]
    Z, rid10, rid21, hand = _text_stage(texts, art)
    hand_n = (hand - art["hand_mu"]) / art["hand_sd"]
    onehot = np.eye(N_DOMAINS)[domain_rule_from_hand(hand)]
    X = np.hstack([Z, hand_n, onehot])

    # quality: similarity kNN over the reference embedding + ridge blend, calibrated
    Eq = _l2n(np.hstack([_l2n(Z), HAND_W * hand_n]))
    sims = Eq.astype(np.float32) @ hs["E_tr"].T
    k = min(KNN_K, sims.shape[1])
    top = np.argpartition(-sims, k - 1, axis=1)[:, :k]
    rows = np.arange(n)[:, None]
    ts = sims[rows, top].astype(np.float64) / KNN_TEMP
    ts -= ts.max(axis=1, keepdims=True)
    w = np.exp(ts)
    w /= w.sum(axis=1, keepdims=True)
    knn10 = (w * hs["m10"][top]).sum(axis=1)

    mu_k, sd_k, mu_r, sd_r = hs["zc"]
    raw10 = 2.0 * (knn10 - mu_k) / sd_k + (rid10 - mu_r) / sd_r
    Em10 = np.interp(raw10, hs["iso10_x"], hs["iso10_y"])
    Em21 = np.interp(rid21, hs["iso21_x"], hs["iso21_y"])

    IN_hat = np.column_stack([np.expm1(tree_eval(hs[f"in{j}"], X)) for j in range(3)])
    OUT75 = np.column_stack([np.expm1(tree_eval(hs[f"o75_{j}"], X)) for j in range(3)])
    OUT85 = np.column_stack([np.expm1(tree_eval(hs[f"o85_{j}"], X)) for j in range(3)])
    return Em10, Em21, IN_hat, OUT75, OUT85


def _predict_worker(texts, conn):
    try:
        conn.send(_predict_core(texts, _FORK_ART))
    finally:
        conn.close()


def _predict_all(texts: Sequence[str], art):
    """Full predictions, forked across the two runtime cores when worthwhile.

    Every prompt is processed independently, so chunking and concatenating
    never changes any result. Plain fork + pipe, no Pool: the official
    sandbox runs with --ipc none (no /dev/shm), which breaks the semaphores
    multiprocessing.Pool needs, while raw pipes keep working.
    """
    global _FORK_ART
    n = len(texts)
    import multiprocessing as mp
    parts = None
    if n >= 64 and sys.platform.startswith("linux"):
        try:
            _FORK_ART = art
            ctx = mp.get_context("fork")
            # interleave so long prompts spread evenly over both workers,
            # then scatter results back to the original positions
            procs, conns = [], []
            for chunk in (texts[0::2], texts[1::2]):
                recv_end, send_end = ctx.Pipe(duplex=False)
                proc = ctx.Process(target=_predict_worker, args=(chunk, send_end))
                proc.start()
                send_end.close()
                procs.append(proc)
                conns.append(recv_end)
            parts = [conn.recv() for conn in conns]
            for proc in procs:
                proc.join()
                if proc.exitcode != 0:
                    parts = None
        except Exception:
            parts = None
        finally:
            _FORK_ART = None
        if parts is not None:
            out = []
            for k in range(5):
                merged = np.empty((n,) + parts[0][k].shape[1:], dtype=parts[0][k].dtype)
                merged[0::2] = parts[0][k]
                merged[1::2] = parts[1][k]
                out.append(merged)
            Em10, Em21, IN_hat, OUT75, OUT85 = out
            return Em10, Em21, IN_hat, {0.75: OUT75, 0.85: OUT85}
    Em10, Em21, IN_hat, OUT75, OUT85 = _predict_core(texts, art)
    return Em10, Em21, IN_hat, {0.75: OUT75, 0.85: OUT85}


def select_model_indices(texts: Sequence[str], tier: str, artifact=None) -> np.ndarray:
    """Per-item decisions for one tier. 0=light, 1=ax31, 2=think."""
    if tier not in TIERS:
        raise ProtocolError(f"알 수 없는 tier: {tier}")
    art = artifact or load_artifact()
    tc = art["tiers"][tier]
    art["_set"] = tc.get("set", "t") if tc.get("set", "t") in art["sets"] else "t"
    Em10, Em21, IN_hat, OUT_hat = _predict_all(texts, art)
    c_sel = (IN_hat * RATE_IN + OUT_hat[tc["q"]] * RATE_OUT) / 1e6
    c_think = (IN_hat[:, 2] * RATE_IN[2] + OUT_hat[tc["qt"]][:, 2] * RATE_OUT[2]) / 1e6
    u = np.stack([
        -tc["lam"] * c_sel[:, 0],
        Em10 - tc["lam"] * c_sel[:, 1],
        Em10 + Em21 - tc["lam_t"] * c_think,
    ], axis=1)
    return u.argmax(axis=1)


def select_decisions(episodes: Sequence[Episode], tier: str, artifact=None) -> List[Decision]:
    texts = [_episode_text(episode) for episode in episodes]
    choice = select_model_indices(texts, tier, artifact)
    return [Decision(ep.episode_id, MODELS[int(choice[i])]) for i, ep in enumerate(episodes)]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="router-run")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--tier", choices=TIERS, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--policy", type=Path)  # accepted but unused, matches the baseline CLI
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        inputs = _FastInput(args.input)
        artifact = load_artifact()
        decisions = select_decisions(inputs.episodes, args.tier, artifact)
        submission = Submission(
            schema_version=inputs.schema_version,
            challenge_id=inputs.challenge_id,
            policy_id=load_bundled_policy().policy_id,
            split=inputs.split,
            tier=args.tier,
            decisions=tuple(decisions),
        )
        write_json(args.output, submission_to_dict(submission))
    except (OSError, ProtocolError, ValueError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
