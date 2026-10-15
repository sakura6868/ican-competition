"""RAG 检索：文档切片 + BM25/余弦混合排序（handoff §8）。

首版使用零外部依赖的本地实现：字符 n-gram 分词（中文场景），BM25 与
词袋余弦混合排序。深度学习中文嵌入作为后续增强，不改接口。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

CHUNK_CHARS = 500
CHUNK_OVERLAP = 80


def split_text(
    text: str, *, chunk_chars: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP
) -> list[str]:
    """按约 chunk_chars 字、overlap 字重叠切片。"""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= chunk_chars:
        return [text] if text else []
    step = max(1, chunk_chars - overlap)
    chunks = [text[i : i + chunk_chars] for i in range(0, len(text), step)]
    return [c for c in chunks if c.strip()]


def _tokenize(text: str) -> list[str]:
    """字符 bigram 分词，适配中文且无外部依赖。"""
    text = text.lower()
    tokens: list[str] = []
    prev = ""
    for ch in text:
        if ch.isalnum():
            if prev:
                tokens.append(prev + ch)
            tokens.append(ch)
            prev = ch
        else:
            prev = ""
    return tokens


class BM25Index:
    """小规模 BM25 检索器（含 IDF 预计算）。"""

    def __init__(self, docs: list[list[str]], k1: float = 1.5, b: float = 0.75) -> None:
        self.docs = docs
        self.k1 = k1
        self.b = b
        self.avgdl = sum(len(d) for d in docs) / max(1, len(docs))
        df: Counter[str] = Counter()
        for d in docs:
            for token in set(d):
                df[token] += 1
        n = len(docs)
        self.idf: dict[str, float] = {}
        for token, freq in df.items():
            self.idf[token] = math.log(1 + (n - freq + 0.5) / (freq + 0.5))

    def score(self, query: list[str], doc: list[str]) -> float:
        if not doc:
            return 0.0
        dl = len(doc)
        tf = Counter(doc)
        total = 0.0
        for term in set(query):
            if term not in self.idf:
                continue
            f = tf.get(term, 0)
            denom = f + self.k1 * (1 - self.b + self.b * dl / self.avgdl)
            total += self.idf[term] * (f * (self.k1 + 1)) / max(denom, 1e-9)
        return total

    def search(self, query: list[str], top_k: int = 6) -> list[tuple[int, float]]:
        scored = [(i, self.score(query, doc)) for i, doc in enumerate(self.docs)]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]


def _cosine_sim(a: Counter[str], b: Counter[str]) -> float:
    dot = sum((a & b).values())
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class HybridRetriever:
    """BM25 + 词袋余弦混合排序。

    返回 [{index, text, bm25, cosine, score}]，score 为归一化混合分。
    """

    def __init__(self, documents: list[str], *, bm25_weight: float = 0.6, top_k: int = 6) -> None:
        self.documents = documents
        self.tokenized = [_tokenize(d) for d in documents]
        self.bm25 = BM25Index(self.tokenized)
        self.bm25_weight = bm25_weight
        self.top_k = top_k

    def search(self, query: str) -> list[dict[str, Any]]:
        q_tokens = _tokenize(query)
        bm25_hits = self.bm25.search(q_tokens, top_k=self.top_k)
        results: list[dict[str, Any]] = []
        max_bm25 = max((s for _, s in bm25_hits), default=0.0) or 1.0
        q_counter = Counter(q_tokens)
        for idx, bm25_score in bm25_hits:
            cosine = _cosine_sim(q_counter, Counter(self.tokenized[idx]))
            norm_bm25 = bm25_score / max_bm25
            score = self.bm25_weight * norm_bm25 + (1 - self.bm25_weight) * cosine
            results.append(
                {
                    "index": idx,
                    "text": self.documents[idx],
                    "bm25": round(bm25_score, 4),
                    "cosine": round(cosine, 4),
                    "score": round(score, 4),
                }
            )
        results.sort(key=lambda r: r["score"], reverse=True)
        return results
