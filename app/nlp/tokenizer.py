"""分词与词项：jieba 词典模式，统一关闭 HMM（实现文档 §1.3/§4.3）。

- 业务词典来自 resources/dictionaries/，停用词来自 resources/stopwords/；
- 停用词仅影响关键词与 BM25 词项，不删除向量输入或答案展示文本中的词语；
- 不得移除"不、未、禁止、除外、至少、最多"等可能改变答案含义的词；
- 禁止使用 jieba.analyse 组件（其默认分词会重新启用 HMM）。
"""

from __future__ import annotations

import hashlib
import unicodedata
from functools import lru_cache
from pathlib import Path

import jieba

RESOURCES_DIR = Path("resources")
DICT_DIR = RESOURCES_DIR / "dictionaries"
STOPWORDS_DIR = RESOURCES_DIR / "stopwords"

# 受保护的否定/条件词：即使出现在停用词文件中也必须保留（实现文档 §4.3）
PROTECTED_WORDS = frozenset(
    {
        "不", "未", "无", "禁止", "不得", "不应", "不能", "除外", "除非",
        "至少", "最多", "低于", "超过", "以上", "以下", "别", "莫", "非",
    }
)


@lru_cache(maxsize=1)
def _ensure_jieba() -> None:
    """进程内一次性初始化：加载业务词典。"""
    jieba.setLogLevel(60)  # 关闭 jieba 的 logger 输出
    if DICT_DIR.is_dir():
        for path in sorted(DICT_DIR.glob("*.dict")):
            jieba.load_userdict(str(path))


@lru_cache(maxsize=1)
def load_stopwords() -> frozenset[str]:
    """加载停用词并剔除受保护词。"""
    words: set[str] = set()
    if STOPWORDS_DIR.is_dir():
        for path in sorted(STOPWORDS_DIR.glob("*.txt")):
            for line in path.read_text(encoding="utf-8").splitlines():
                word = line.strip()
                if word and not word.startswith("#"):
                    words.add(word)
    return frozenset(words - PROTECTED_WORDS)


def resource_fingerprint() -> str:
    """词典与停用词内容指纹，参与 pipeline_version。"""
    digest = hashlib.sha256()
    for directory in (DICT_DIR, STOPWORDS_DIR):
        if directory.is_dir():
            for path in sorted(directory.rglob("*")):
                if path.is_file():
                    digest.update(str(path).encode("utf-8"))
                    digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


def tokenize(text: str) -> list[str]:
    """jieba 词典模式分词，HMM 强制关闭；不过滤停用词。"""
    _ensure_jieba()
    return [t for t in jieba.lcut(text, HMM=False) if t.strip()]


def content_terms(text: str) -> list[str]:
    """BM25/关键词使用的词项：分词后过滤停用词与纯标点。"""
    _ensure_jieba()
    stopwords = load_stopwords()
    terms: list[str] = []
    for token in jieba.lcut(text, HMM=False):
        token = token.strip()
        if not token or token in stopwords:
            continue
        if all(unicodedata.category(ch).startswith(("P", "Z", "C")) for ch in token):
            continue
        terms.append(token)
    return terms
