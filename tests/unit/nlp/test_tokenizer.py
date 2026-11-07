"""分词单测：HMM 强制关闭回归、停用词保护、资源指纹。"""

import re

import jieba

from app.nlp.tokenizer import (
    PROTECTED_WORDS,
    content_terms,
    load_stopwords,
    resource_fingerprint,
    tokenize,
)


def test_tokenize_matches_hmm_off():
    """回归：tokenize 必须与 jieba.lcut(HMM=False) 完全一致（实现文档 §1.3）。"""
    text = "员工应在出差结束后30日内申请报销，系统 v2.1.3 限额 500元。"
    assert tokenize(text) == [t for t in jieba.lcut(text, HMM=False) if t.strip()]


def test_content_terms_filters_punct():
    terms = content_terms("员工 应在 30 日内申请。")
    assert "。" not in terms
    assert "员工" in terms
    assert "30" in terms


def test_stopwords_only_affect_terms():
    # tokenize 不过滤任何词；content_terms 过滤标点与停用词（实现文档 §4.3）
    text = "员工在规定时间内提交材料。"
    assert tokenize(text) == [t for t in jieba.lcut(text, HMM=False) if t.strip()]
    assert "。" not in content_terms(text)


def test_protected_words_never_removed():
    # 即使将来停用词文件包含否定词，也必须被剔除出停用词表（实现文档 §4.3）
    assert not (PROTECTED_WORDS & load_stopwords())


def test_resource_fingerprint_shape():
    assert re.fullmatch(r"[0-9a-f]{12}", resource_fingerprint())
