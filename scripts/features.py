# SPDX-License-Identifier: Apache-2.0
"""Phase 1: deterministic prompt featurizer.

Design constraints (final runtime: linux/arm64, 2 CPU cores, 2GiB, no sklearn):
- fit() may use anything (runs offline at training time).
- transform() uses only stdlib + numpy so the container path can import this
  module directly. No hashing tricks: vocabulary + idf are explicit, so the
  same prompt always maps to the same vector regardless of batch order.

Blocks
  wv   : word 1-2 gram TF-IDF (sublinear tf, l2)          sparse, vocab-based
  cv   : char_wb 3-5 gram TF-IDF (sublinear tf, l2)       sparse, vocab-based
  hand : ~40 engineered scalars (length, Korean ratio, code/math density, ...)
  dense: [SVD(wv|cv) | scaled hand | domain one-hot]      for kNN / GBDT / MLP
"""
from __future__ import annotations

import json
import re
import unicodedata
from typing import Dict, List, Sequence, Tuple

import numpy as np

# ---------------------------------------------------------------- tokenization

_WORD_RE = re.compile(r"[A-Za-z]+|[가-힣]+|[0-9]+|[^\sA-Za-z가-힣0-9]")


def word_tokens(text: str) -> List[str]:
    return _WORD_RE.findall(text.lower())


def word_ngrams(text: str) -> List[str]:
    toks = word_tokens(text)
    grams = list(toks)
    grams.extend(f"{a} {b}" for a, b in zip(toks, toks[1:]))
    return grams


def char_wb_ngrams(text: str, lo: int = 3, hi: int = 5) -> List[str]:
    grams: List[str] = []
    for tok in text.lower().split():
        padded = f" {tok} "
        L = len(padded)
        for n in range(lo, hi + 1):
            if L <= n:
                grams.append(padded)
                break
            grams.extend(padded[i:i + n] for i in range(L - n + 1))
    return grams


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


def hand_feature_names() -> List[str]:
    return [
        "len", "log_len", "n_words", "avg_word_len", "n_lines", "n_newline",
        "digit_frac", "max_digit_run", "korean_frac", "ascii_alpha_frac",
        "upper_frac", "space_frac", "punct_frac",
        "code_cnt", "code_density", "brace_cnt", "semicolon_cnt", "indent_lines",
        "math_cnt", "math_density", "dollar_cnt", "backslash_cnt", "op_density",
        "mcq_cnt", "explain_cnt", "transl_cnt", "compute_cnt", "aime_cnt",
        "qmark_cnt", "ends_qmark", "n_sentences", "tok_est",
    ]


def hand_features(texts: Sequence[str]) -> np.ndarray:
    rows = []
    for text in texts:
        t = str(text)
        low = t.lower()
        L = len(t) + 1
        words = t.split()
        lines = t.split("\n")
        n_digit = sum(c.isdigit() for c in t)
        runs = re.findall(r"[0-9]+", t)
        korean = sum(1 for c in t if "가" <= c <= "힣")
        ascii_a = sum(1 for c in t if c.isascii() and c.isalpha())
        upper = sum(1 for c in t if c.isupper())
        space = t.count(" ")
        punct = sum(1 for c in t if unicodedata.category(c).startswith("P"))
        code_cnt = sum(low.count(m) for m in _CODE_MARKERS)
        math_cnt = sum(low.count(m) for m in _MATH_MARKERS)
        mcq_cnt = sum(low.count(m) for m in _MCQ_MARKERS)
        ops = sum(c in "+-*/=^<>%" for c in t)
        indent = sum(1 for ln in lines if ln.startswith(("    ", "\t")))
        # crude token estimate: ascii ~4 chars/token, korean ~1.7 chars/token
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


DOMAINS = ["code", "math", "korean", "mcq", "english"]


def domain_rule(texts: Sequence[str]) -> np.ndarray:
    """Rule-based domain id per prompt (precedence: code > math > mcq > korean > english)."""
    hf = hand_features(texts)
    names = hand_feature_names()
    col = {n: i for i, n in enumerate(names)}
    out = []
    for i in range(len(hf)):
        code = hf[i, col["code_cnt"]] >= 2 or (hf[i, col["brace_cnt"]] >= 4 and hf[i, col["semicolon_cnt"]] >= 2)
        math = hf[i, col["math_cnt"]] >= 3 or hf[i, col["dollar_cnt"]] >= 4 or hf[i, col["aime_cnt"]] >= 1
        mcq = hf[i, col["mcq_cnt"]] >= 3
        kor = hf[i, col["korean_frac"]] > 0.05
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

class VocabTfidf:
    """Vocabulary-based TF-IDF with sublinear tf and l2 norm. Pure numpy transform."""

    def __init__(self, analyzer, min_df: int = 3, max_features: int = 60000):
        self.analyzer = analyzer
        self.min_df = min_df
        self.max_features = max_features
        self.vocab: Dict[str, int] = {}
        self.idf: np.ndarray | None = None

    def fit(self, texts: Sequence[str]) -> "VocabTfidf":
        df: Dict[str, int] = {}
        for text in texts:
            for g in set(self.analyzer(text)):
                df[g] = df.get(g, 0) + 1
        items = [(g, c) for g, c in df.items() if c >= self.min_df]
        items.sort(key=lambda x: (-x[1], x[0]))
        items = items[: self.max_features]
        items.sort(key=lambda x: x[0])  # stable, order-independent ids
        self.vocab = {g: i for i, (g, _) in enumerate(items)}
        n = len(texts)
        dfv = np.array([df[g] for g, _ in items], dtype=np.float64)
        self.idf = np.log((1.0 + n) / (1.0 + dfv)) + 1.0
        return self

    def transform_rows(self, texts: Sequence[str]) -> List[Dict[int, float]]:
        """Sparse rows as {col: value} after sublinear tf * idf and l2 norm."""
        rows = []
        for text in texts:
            counts: Dict[int, int] = {}
            for g in self.analyzer(text):
                j = self.vocab.get(g)
                if j is not None:
                    counts[j] = counts.get(j, 0) + 1
            vals = {j: (1.0 + np.log(c)) * self.idf[j] for j, c in counts.items()}
            norm = np.sqrt(sum(v * v for v in vals.values())) or 1.0
            rows.append({j: v / norm for j, v in vals.items()})
        return rows

    def transform_csr(self, texts: Sequence[str]):
        from scipy.sparse import csr_matrix
        rows = self.transform_rows(texts)
        indptr, indices, data = [0], [], []
        for r in rows:
            for j in sorted(r):
                indices.append(j)
                data.append(r[j])
            indptr.append(len(indices))
        return csr_matrix((data, indices, indptr), shape=(len(rows), len(self.vocab)))


class Featurizer:
    """Full pipeline: fit on train texts, transform anywhere (numpy at runtime)."""

    def __init__(self, svd_dim: int = 256, word_max: int = 40000, char_max: int = 80000):
        self.wv = VocabTfidf(word_ngrams, min_df=3, max_features=word_max)
        self.cv = VocabTfidf(char_wb_ngrams, min_df=4, max_features=char_max)
        self.svd_dim = svd_dim
        self.P: np.ndarray | None = None       # (n_wv+n_cv, svd_dim)
        self.hand_mu: np.ndarray | None = None
        self.hand_sd: np.ndarray | None = None

    def fit(self, texts: Sequence[str]) -> "Featurizer":
        self.wv.fit(texts)
        self.cv.fit(texts)
        from scipy.sparse import hstack
        from sklearn.decomposition import TruncatedSVD
        X = hstack([self.wv.transform_csr(texts), self.cv.transform_csr(texts)]).tocsr()
        svd = TruncatedSVD(n_components=self.svd_dim, random_state=0)
        svd.fit(X)
        self.P = svd.components_.T.astype(np.float64)
        hf = hand_features(texts)
        self.hand_mu = hf.mean(axis=0)
        self.hand_sd = hf.std(axis=0) + 1e-9
        return self

    # -- runtime path: stdlib + numpy only ------------------------------------
    def sparse_rows(self, texts: Sequence[str]) -> Tuple[List[Dict[int, float]], List[Dict[int, float]]]:
        return self.wv.transform_rows(texts), self.cv.transform_rows(texts)

    def dense(self, texts: Sequence[str]) -> np.ndarray:
        wr, cr = self.sparse_rows(texts)
        off = len(self.wv.vocab)
        Z = np.zeros((len(texts), self.svd_dim))
        for i, (a, b) in enumerate(zip(wr, cr)):
            for j, v in a.items():
                Z[i] += v * self.P[j]
            for j, v in b.items():
                Z[i] += v * self.P[off + j]
        hf = (hand_features(texts) - self.hand_mu) / self.hand_sd
        dom = domain_rule(texts)
        onehot = np.eye(len(DOMAINS))[dom]
        return np.hstack([Z, hf, onehot])

    # -- train-time path: scipy sparse for linear heads -----------------------
    def sparse(self, texts: Sequence[str]):
        from scipy.sparse import hstack
        return hstack([self.wv.transform_csr(texts), self.cv.transform_csr(texts)]).tocsr()

    def save(self, path: str) -> None:
        np.savez_compressed(
            path,
            P=self.P.astype(np.float32),
            idf_w=self.wv.idf, idf_c=self.cv.idf,
            hand_mu=self.hand_mu, hand_sd=self.hand_sd,
            vocab_w=json.dumps(self.wv.vocab), vocab_c=json.dumps(self.cv.vocab),
            svd_dim=self.svd_dim,
        )

    @classmethod
    def load(cls, path: str) -> "Featurizer":
        z = np.load(path, allow_pickle=False)
        f = cls(svd_dim=int(z["svd_dim"]))
        f.wv.vocab = {k: int(v) for k, v in json.loads(str(z["vocab_w"])).items()}
        f.cv.vocab = {k: int(v) for k, v in json.loads(str(z["vocab_c"])).items()}
        f.wv.idf = z["idf_w"]
        f.cv.idf = z["idf_c"]
        f.P = z["P"].astype(np.float64)
        f.hand_mu = z["hand_mu"]
        f.hand_sd = z["hand_sd"]
        return f
