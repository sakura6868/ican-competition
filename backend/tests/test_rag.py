"""RAG 检索测试。"""

from __future__ import annotations

from gongxu.rag.retriever import BM25Index, HybridRetriever, _tokenize, split_text


def test_split_text_no_op_for_short() -> None:
    assert split_text("短文本") == ["短文本"]


def test_split_text_overlap() -> None:
    text = "工序步骤说明。" * 200  # 1600 字
    chunks = split_text(text, chunk_chars=500, overlap=80)
    assert len(chunks) >= 3
    # 相邻切片应有重叠（约 80 字）
    assert chunks[0][-80:] == chunks[1][:80]


def test_tokenize_chinese_bigram() -> None:
    tokens = _tokenize("拔掉插头")
    assert "插头" in tokens
    assert "拔掉" in tokens


def test_bm25_ranks_relevant_first() -> None:
    docs = [
        _tokenize("安全操作规程：断电后必须拔掉电源插头"),
        _tokenize("外观检查：砂轮无裂纹"),
        _tokenize("其他无关内容"),
    ]
    idx = BM25Index(docs)
    hits = idx.search(_tokenize("断电 拔掉 插头"))
    assert hits[0][0] == 0


def test_hybrid_retriever_relevant_first() -> None:
    docs = [
        "安全操作规程：断电后必须拔掉电源插头，等待砂轮完全停转",
        "外观检查：砂轮无裂纹、无磨损超标",
        "无关内容",
    ]
    retriever = HybridRetriever(docs, top_k=3)
    results = retriever.search("断电 拔掉插头")
    assert results[0]["index"] == 0
    assert results[0]["score"] > results[1]["score"]
    assert {"index", "text", "bm25", "cosine", "score"} <= set(results[0].keys())
