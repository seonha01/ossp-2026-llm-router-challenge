# SPDX-License-Identifier: Apache-2.0
"""Prompt feature extraction for the runtime router.

stdlib + numpy only. Produces byte-for-byte the same feature values as the
training-time featurizer (scripts/features.py); only the computation strategy
differs (per-codepoint property memoization, per-token vocabulary memoization)
so it stays fast on the 2-core arm64 runtime. Any semantic change here breaks
the frozen calibration.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from typing import Dict, List, Sequence, Tuple

import numpy as np

_WORD_RE = re.compile(r"[A-Za-z]+|[가-힣]+|[0-9]+|[^\sA-Za-z가-힣0-9]")

# ---------------------------------------------------------------- hand features

_CODE_MARKERS = [
    "def ", "return", "import ", "assert", "class ", "print(", "();", "==",
    "#include", "public ", "static ", "void ", "select ", "from ", "where ",
    "for (", "for(", "while", "function", "console.log", "lambda", "->",
    "np.", "pd.", "self.", "”””", '"""', "```",
]
_MATH_MARKERS = [
    "\\frac", "\\sum", "\\int", "\\sqrt", "\\cdot", "\\times", "\\le", "\\ge",
    "\\pi", "\\alpha", "\\beta", "\\theta", "\\(", "\\[", "$", "^{", "_{",
    "sqrt", "integer", "remainder", "divisible", "prime", "triangle", "polynomial",
    "정수", "소수", "나머지", "방정식", "삼각형", "확률",
]
_MCQ_MARKERS = ["①", "②", "③", "④", "⑤", "(a)", "(b)", "(c)", "(d)",
                "a.", "b.", "c.", "d.", "다음 중", "고르", "옳은 것", "틀린 것"]
_ASK_EXPLAIN = ["why", "how", "explain", "describe", "discuss", "summar",
                "설명", "요약", "서술", "이유", "왜"]
_ASK_TRANSL = ["translate", "번역", "영어로", "한국어로", "우리말로"]
_ASK_COMPUTE = ["compute", "calculate", "find the", "how many", "what is the value",
                "구하시오", "구하라", "계산", "값은", "몇 "]
_AIME_MARKERS = ["aime", "positive integer", "let $", "let \\(", "find the number of",
                 "modulo 1000", "0 and 999", "between 0 and 999"]

# per-codepoint character classes, computed once and memoized
# (digit, upper, punct, ascii_alpha, korean, op)
_CP_PROPS: Dict[int, Tuple[int, int, int, int, int, int]] = {}
_OPS = set("+-*/=^<>%")


def _cp_props(cp: int):
    hit = _CP_PROPS.get(cp)
    if hit is None:
        ch = chr(cp)
        hit = (
            int(ch.isdigit()),
            int(ch.isupper()),
            int(unicodedata.category(ch).startswith("P")),
            int(ch.isascii() and ch.isalpha()),
            int("가" <= ch <= "힣"),
            int(ch in _OPS),
        )
        _CP_PROPS[cp] = hit
    return hit


def _char_class_counts(t: str):
    """(n_digit, n_upper, n_punct, n_ascii_alpha, n_korean, n_ops) — exact."""
    if not t:
        return 0, 0, 0, 0, 0, 0
    arr = np.frombuffer(t.encode("utf-32-le"), dtype=np.uint32)
    uniq, cnt = np.unique(arr, return_counts=True)
    tot = np.zeros(6, dtype=np.int64)
    for cp, c in zip(uniq.tolist(), cnt.tolist()):
        p = _cp_props(cp)
        if p != (0, 0, 0, 0, 0, 0):
            tot += np.array(p, dtype=np.int64) * c
    return tuple(tot.tolist())


def hand_features(texts: Sequence[str]) -> np.ndarray:
    rows = []
    for text in texts:
        t = str(text)
        low = t.lower()
        L = len(t) + 1
        words = t.split()
        lines = t.split("\n")
        n_digit, upper, punct, ascii_a, korean, ops = _char_class_counts(t)
        runs = re.findall(r"[0-9]+", t)
        space = t.count(" ")
        code_cnt = sum(low.count(m) for m in _CODE_MARKERS)
        math_cnt = sum(low.count(m) for m in _MATH_MARKERS)
        mcq_cnt = sum(low.count(m) for m in _MCQ_MARKERS)
        indent = sum(1 for ln in lines if ln.startswith(("    ", "\t")))
        tok_est = (L - korean) / 4.0 + korean / 1.7
        rows.append([
            len(t), np.log1p(len(t)), len(words),
            (sum(len(w) for w in words) / (len(words) + 1)),
            len(lines), t.count("\n"),
            n_digit / L, max((len(r) for r in runs), default=0),
            korean / L, ascii_a / L, upper / L, space / L, punct / L,
            code_cnt, code_cnt / L * 100, t.count("{") + t.count("}"),
            t.count(";"), indent,
            math_cnt, math_cnt / L * 100, t.count("$"), t.count("\\"),
            ops / L,
            mcq_cnt, sum(low.count(m) for m in _ASK_EXPLAIN),
            sum(low.count(m) for m in _ASK_TRANSL),
            sum(low.count(m) for m in _ASK_COMPUTE),
            sum(low.count(m) for m in _AIME_MARKERS),
            t.count("?"), int(t.rstrip().endswith("?")),
            len(re.findall(r"[.!?]\s", t)) + 1, tok_est,
        ])
    return np.array(rows, dtype=np.float64)


_HAND_COL = {n: i for i, n in enumerate([
    "len", "log_len", "n_words", "avg_word_len", "n_lines", "n_newline",
    "digit_frac", "max_digit_run", "korean_frac", "ascii_alpha_frac",
    "upper_frac", "space_frac", "punct_frac",
    "code_cnt", "code_density", "brace_cnt", "semicolon_cnt", "indent_lines",
    "math_cnt", "math_density", "dollar_cnt", "backslash_cnt", "op_density",
    "mcq_cnt", "explain_cnt", "transl_cnt", "compute_cnt", "aime_cnt",
    "qmark_cnt", "ends_qmark", "n_sentences", "tok_est",
])}

N_DOMAINS = 5


def domain_rule_from_hand(hf: np.ndarray) -> np.ndarray:
    c = _HAND_COL
    out = []
    for i in range(len(hf)):
        code = hf[i, c["code_cnt"]] >= 2 or (hf[i, c["brace_cnt"]] >= 4 and hf[i, c["semicolon_cnt"]] >= 2)
        math = hf[i, c["math_cnt"]] >= 3 or hf[i, c["dollar_cnt"]] >= 4 or hf[i, c["aime_cnt"]] >= 1
        mcq = hf[i, c["mcq_cnt"]] >= 3
        kor = hf[i, c["korean_frac"]] > 0.05
        if code:
            out.append(0)
        elif math:
            out.append(1)
        elif mcq:
            out.append(3)
        elif kor:
            out.append(2)
        else:
            out.append(4)
    return np.array(out, dtype=np.int64)


# ---------------------------------------------------------------- tfidf blocks

_EMPTY = (np.empty(0, dtype=np.int64), np.empty(0, dtype=np.float64))


class BlockVectorizer:
    """One tf-idf block (word or char_wb) with per-token vocabulary memoization.

    Gram counting is identical to the training featurizer: char_wb grams are
    generated per whitespace token independently, word grams from the regex
    token stream, then sublinear tf * idf and an l2 norm over the block.
    """

    def __init__(self, vocab: Dict[str, int], idf: np.ndarray, kind: str,
                 offset: int = 0, memo_cap: int = 150000):
        self.vocab = vocab
        self.idf = idf
        self.kind = kind
        self.offset = offset
        self.memo_cap = memo_cap
        self._tok_memo: Dict[str, Tuple[np.ndarray, np.ndarray]] = {}

    def _char_token_cols(self, token: str):
        hit = self._tok_memo.get(token)
        if hit is None:
            counts: Dict[int, int] = {}
            get = self.vocab.get
            padded = f" {token} "
            L = len(padded)
            for n in (3, 4, 5):
                if L <= n:
                    j = get(padded)
                    if j is not None:
                        counts[j] = counts.get(j, 0) + 1
                    break
                for i in range(L - n + 1):
                    j = get(padded[i:i + n])
                    if j is not None:
                        counts[j] = counts.get(j, 0) + 1
            hit = (np.fromiter(counts.keys(), np.int64, len(counts)),
                   np.fromiter(counts.values(), np.float64, len(counts)))
            if len(self._tok_memo) < self.memo_cap:
                self._tok_memo[token] = hit
        return hit

    def row(self, text: str) -> Tuple[np.ndarray, np.ndarray]:
        """(global cols, l2-normalized sublinear tfidf values) for one prompt."""
        low = text.lower()
        if self.kind == "char":
            cols_parts, cnt_parts = [], []
            for token, k in Counter(low.split()).items():
                cols, cnts = self._char_token_cols(token)
                if len(cols):
                    cols_parts.append(cols)
                    cnt_parts.append(cnts * k)
            if not cols_parts:
                return _EMPTY
            cat_cols = np.concatenate(cols_parts)
            cat_cnts = np.concatenate(cnt_parts)
            cols, inv = np.unique(cat_cols, return_inverse=True)
            cnt = np.bincount(inv, weights=cat_cnts)
        else:
            toks = _WORD_RE.findall(low)
            grams = Counter(toks)
            grams.update(f"{a} {b}" for a, b in zip(toks, toks[1:]))
            get = self.vocab.get
            cols_l, cnt_l = [], []
            for g, k in grams.items():
                j = get(g)
                if j is not None:
                    cols_l.append(j)
                    cnt_l.append(k)
            if not cols_l:
                return _EMPTY
            cols = np.array(cols_l, dtype=np.int64)
            cnt = np.array(cnt_l, dtype=np.float64)
        vals = (1.0 + np.log(cnt)) * self.idf[cols]
        norm = np.sqrt((vals * vals).sum())
        vals /= norm if norm > 0 else 1.0
        return cols + self.offset, vals


def word_tokens(text: str) -> List[str]:
    return _WORD_RE.findall(text.lower())


# ------------------------------------------------------ packed word matching

class WordPackedVectorizer:
    """word 1-2 gram tf-idf block, numpy-vectorized with exact token-id keys.

    Every token that occurs in any vocabulary gram gets an id >= 1; a prompt
    token outside that universe maps to 0, and no vocabulary key contains a
    0 component, so those grams simply never match. Bigram keys carry a flag
    bit so they can never collide with unigram keys. Prompts with an unusually
    long token fall back to the reference python path (identical results).
    """

    def __init__(self, vocab: Dict[str, int], idf: np.ndarray, offset: int,
                 max_token_chars: int = 48):
        self.fallback = BlockVectorizer(vocab, idf, "word", offset)
        self.idf = idf
        self.offset = offset
        self.max_token_chars = max_token_chars
        self.tid: Dict[str, int] = {}
        keys, cols = [], []
        for g, c in vocab.items():
            parts = g.split(" ")
            for p in parts:
                if p not in self.tid:
                    self.tid[p] = len(self.tid) + 1
            if len(parts) == 1:
                keys.append(self.tid[parts[0]])
            else:
                keys.append((1 << 40) | (self.tid[parts[0]] << 21) | self.tid[parts[1]])
            cols.append(c)
        keys = np.array(keys, dtype=np.int64)
        cols = np.array(cols, dtype=np.int64)
        order = np.argsort(keys)
        self.vkeys = keys[order]
        self.vcols = cols[order]

    def row(self, text: str) -> Tuple[np.ndarray, np.ndarray]:
        toks = _WORD_RE.findall(text.lower())
        if not toks:
            return _EMPTY
        if max(map(len, toks)) > self.max_token_chars:
            return self.fallback.row(text)
        arr = np.array(toks)
        uniq, inv = np.unique(arr, return_inverse=True)
        get = self.tid.get
        ids = np.array([get(t, 0) for t in uniq.tolist()], dtype=np.int64)[inv]
        parts = [ids]
        if len(ids) > 1:
            parts.append((1 << 40) | (ids[:-1] << 21) | ids[1:])
        keys = np.concatenate(parts)
        ukeys, cnt = np.unique(keys, return_counts=True)
        j = np.searchsorted(self.vkeys, ukeys)
        j_safe = np.minimum(j, len(self.vkeys) - 1)
        ok = self.vkeys[j_safe] == ukeys
        cols = self.vcols[j_safe[ok]]
        cnt = cnt[ok].astype(np.float64)
        if not len(cols):
            return _EMPTY
        vals = (1.0 + np.log(cnt)) * self.idf[cols]
        norm = np.sqrt((vals * vals).sum())
        vals /= norm if norm > 0 else 1.0
        return cols + self.offset, vals


# ---------------------------------------------------- packed char_wb matching

def pack_gram(gram: str) -> Tuple[int, int]:
    """Pack a 3..5-char gram into two int64 keys, collision-free.

    Codepoints are < 2^21, so (c0|c1<<21|c2<<42, len<<42|c3|c4<<21) is exact.
    """
    cp = [ord(c) for c in gram]
    L = len(cp)
    k1 = cp[0] | (cp[1] << 21) | (cp[2] << 42)
    k2 = L << 42
    if L > 3:
        k2 |= cp[3]
    if L > 4:
        k2 |= cp[4] << 21
    return k1, k2


def pack_vocab(vocab: Dict[str, int]):
    """(vk1, vk2, vcol) sorted by (k1, k2) for searchsorted matching."""
    n = len(vocab)
    k1 = np.empty(n, np.int64)
    k2 = np.empty(n, np.int64)
    col = np.empty(n, np.int64)
    for i, (g, c) in enumerate(vocab.items()):
        a, b = pack_gram(g)
        k1[i], k2[i], col[i] = a, b, c
    order = np.lexsort((k2, k1))
    return k1[order], k2[order], col[order]


class CharPackedVectorizer:
    """char_wb 3-5 gram tf-idf block computed with pure numpy.

    Equivalent to per-token generation: tokens are joined with DOUBLE spaces,
    so any sliding window that crosses a token boundary contains "  ", which
    no vocabulary gram can contain (tokens hold no whitespace and padding adds
    single spaces) — those windows simply never match. Windows inside a padded
    token reproduce the per-token grams exactly, including the short-token
    whole-gram rule.
    """

    def __init__(self, vk1, vk2, vcol, idf: np.ndarray, offset: int):
        # collapse (k1, k2) into one sortable int64: rank(k1) << 45 | k2.
        # rank < 2^17 and k2 < 2^45, so the combined key is exact and unique.
        self.vu1 = np.unique(vk1)
        rank = np.searchsorted(self.vu1, vk1)
        self.vcomb = (rank << 45) | vk2
        order = np.argsort(self.vcomb)
        self.vcomb = self.vcomb[order]
        self.vcol = vcol[order]
        self.idf = idf
        self.offset = offset

    def row(self, text: str) -> Tuple[np.ndarray, np.ndarray]:
        toks = text.lower().split()
        if not toks:
            return _EMPTY
        S = " " + "  ".join(toks) + " "
        arr = np.frombuffer(S.encode("utf-32-le"), dtype=np.uint32).astype(np.int64)
        N = arr.size
        k13 = arr[:-2] | (arr[1:-1] << 21) | (arr[2:] << 42)
        K1 = [k13]
        K2 = [np.full(N - 2, 3 << 42, dtype=np.int64)]
        if N >= 4:
            K1.append(k13[: N - 3])
            K2.append((4 << 42) | arr[3:])
        if N >= 5:
            K1.append(k13[: N - 4])
            K2.append((5 << 42) | arr[3:N - 1] | (arr[4:] << 21))
        c1 = np.concatenate(K1)
        c2 = np.concatenate(K2)
        order = np.lexsort((c2, c1))
        s1, s2 = c1[order], c2[order]
        new = np.empty(len(s1), bool)
        new[0] = True
        np.logical_or(s1[1:] != s1[:-1], s2[1:] != s2[:-1], out=new[1:])
        starts = np.flatnonzero(new)
        cnt = np.diff(np.append(starts, len(s1))).astype(np.float64)
        uk1, uk2 = s1[starts], s2[starts]

        r = np.searchsorted(self.vu1, uk1)
        r_safe = np.minimum(r, len(self.vu1) - 1)
        valid = self.vu1[r_safe] == uk1
        comb = (r_safe << 45) | uk2
        j = np.searchsorted(self.vcomb, comb)
        j_safe = np.minimum(j, len(self.vcomb) - 1)
        ok = valid & (self.vcomb[j_safe] == comb)
        cols = self.vcol[j_safe[ok]]
        cnt = cnt[ok]
        if not len(cols):
            return _EMPTY
        vals = (1.0 + np.log(cnt)) * self.idf[cols]
        norm = np.sqrt((vals * vals).sum())
        vals /= norm if norm > 0 else 1.0
        return cols + self.offset, vals
