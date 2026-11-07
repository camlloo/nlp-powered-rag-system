"""BM25 单测：IDF 公式、评分口径、确定性 tie-break（实现文档 §7.2）。"""

import math

import pytest

from app.retrieval.bm25 import BM25Index


def _build() -> BM25Index:
    return BM25Index.build(
        [101, 102, 103],
        [
            ["报销", "时限"],
            ["报销", "流程"],
            ["考勤"],
        ],
    )


def test_idf_formula():
    index = _build()
    # df(报销)=2, N=3: log(1 + (3-2+0.5)/(2+0.5)) = log(1.6)
    assert index.idf["报销"] == pytest.approx(math.log(1.6))
    # df(考勤)=1: log(1 + 2.5/1.5)
    assert index.idf["考勤"] == pytest.approx(math.log(1 + 2.5 / 1.5))


def test_scoring_matches_manual_formula():
    index = _build()
    k1, b = index.k1, index.b
    hits = index.search(["报销"], k=10)
    scores = dict(hits)
    idf = index.idf["报销"]
    avgdl = index.avgdl
    for pos, cid in [(0, 101), (1, 102)]:
        tf = 1.0
        dl_ratio = index.doc_len[pos] / avgdl
        expected = idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * dl_ratio))
        assert scores[cid] == pytest.approx(expected)
    assert 103 not in scores  # 未命中查询词的块不得出现


def test_tie_break_by_chunk_id():
    index = _build()
    hits = index.search(["报销"])
    assert [chunk_id for chunk_id, _ in hits] == [101, 102]  # 同分按 chunk_id 升序


def test_unknown_term_and_empty_query():
    index = _build()
    assert index.search(["不存在"]) == []
    assert index.search([]) == []


def test_query_term_frequency_counts():
    index = BM25Index.build([1], [["报销", "报销", "流程"]])
    hits = index.search(["报销", "报销"], k=1)
    # 查询词集合去重（set），两次提及不重复计分
    assert len(hits) == 1
