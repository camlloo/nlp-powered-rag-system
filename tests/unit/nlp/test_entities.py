"""规则实体抽取单测（实现文档 §4.3）。"""

from app.nlp.entities import entity_signature, extract_entities


def test_date_entities():
    types = {e.text for e in extract_entities("2026年11月7日 与 30 日内 生效")}
    assert "2026年11月7日" in types
    assert any("30" in t for t in types)


def test_money_and_percent():
    text = "限额 500元，超出部分按 12.5% 计收，预存 ¥1,000.50"
    texts = {e.text for e in extract_entities(text)}
    assert "500元" in texts
    assert "12.5%" in texts
    assert "¥1,000.50" in texts


def test_version_and_ip():
    texts = {e.text for e in extract_entities("网关 v2.1.3 部署于 192.168.1.1")}
    assert "v2.1.3" in texts
    assert "192.168.1.1" in texts


def test_entity_positions():
    entities = extract_entities("限额为 500元")
    target = next(e for e in entities if e.text == "500元")
    assert text_slice("限额为 500元", target.start, target.end) == "500元"


def test_signature_detects_money_change():
    assert entity_signature("限额 500元") != entity_signature("限额 600元")


def test_signature_stable_for_same_text():
    assert entity_signature("限额 500元") == entity_signature("限额 500元")


def text_slice(text: str, start: int, end: int) -> str:
    return text[start:end]
