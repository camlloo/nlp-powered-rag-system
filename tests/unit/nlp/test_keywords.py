"""TF-IDF 关键词单测：确定性、数量边界、IDF 口径（实现文档 §4.4）。"""

import math

from app.nlp.keywords import corpus_idf, extract_keywords, top_keywords


def test_idf_formula_matches_bm25():
    docs = [["a", "b"], ["a"], ["c", "a"]]
    idf = corpus_idf(docs)
    # df(a)=3, N=3: log(1 + (3-3+0.5)/(3+0.5))
    assert math.isclose(idf["a"], math.log(1 + 0.5 / 3.5))
    # df(c)=1: log(1 + (3-1+0.5)/(1+0.5))
    assert math.isclose(idf["c"], math.log(1 + 2.5 / 1.5))


def test_top_keywords_sorted_and_bounded():
    tokens = ["报销", "报销", "报销", "流程", "流程", "票据", "审批", "期限", "系统", "版本"]
    idf = {t: 1.0 for t in set(tokens)}
    kws = top_keywords(tokens, idf)
    assert 1 <= len(kws) <= 8
    assert kws[0] == "报销"


def test_extract_keywords_per_chunk():
    docs = [
        ["报销", "票据", "流程", "期限", "审批", "额度"],
        ["考勤", "签到", "迟到", "旷工", "处理", "记录"],
    ]
    results = extract_keywords(docs)
    assert len(results) == 2
    assert "报销" in results[0]
    assert "考勤" in results[1]


def test_few_unique_terms_returns_all():
    kws = top_keywords(["报销", "报销"], {"报销": 1.0})
    assert kws == ["报销"]


def test_deterministic_tie_break():
    tokens = ["甲", "乙", "丙"]
    idf = {t: 1.0 for t in tokens}
    assert top_keywords(tokens, idf) == top_keywords(tokens, idf)
