"""规则分句单测：保护模式与引号边界（每条规则配样本，实现文档 §4.3）。"""

from app.nlp.sentence import split_sentences


def test_basic_split_keeps_terminal_punct():
    assert split_sentences("第三章 费用报销。员工应在出差结束后 30 日内申请。") == [
        "第三章 费用报销。",
        "员工应在出差结束后 30 日内申请。",
    ]


def test_decimal_protected():
    assert split_sentences("单价为 30.5 元。次日生效。") == ["单价为 30.5 元。", "次日生效。"]


def test_version_protected():
    assert split_sentences("版本 v2.1.3。已发布。") == ["版本 v2.1.3。", "已发布。"]


def test_ip_protected():
    assert split_sentences("服务器地址是 192.168.1.1。请勿外传。") == [
        "服务器地址是 192.168.1.1。",
        "请勿外传。",
    ]


def test_url_protected():
    assert split_sentences("参见 https://a.com/p?q=1。完毕。") == [
        "参见 https://a.com/p?q=1。",
        "完毕。",
    ]


def test_abbreviation_protected():
    assert split_sentences("例如 e.g. 苹果。到此为止。") == [
        "例如 e.g. 苹果。",
        "到此为止。",
    ]


def test_quote_boundary():
    # 句号在引号内：不切；引号闭合后的句号才切
    text = "规则如下：“金额为 500。上限另计。”其他同前。"
    assert split_sentences(text) == [text]


def test_english_sentences():
    assert split_sentences("Submit within 30 days. Keep receipts.") == [
        "Submit within 30 days.",
        " Keep receipts.",
    ]


def test_no_terminal_punct_returns_whole():
    assert split_sentences("没有句尾标点的文本") == ["没有句尾标点的文本"]


def test_empty():
    assert split_sentences("") == []
    assert split_sentences("   ") == []
