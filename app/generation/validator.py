"""答案校验（实现文档 §8.3）。

校验内容：非空、引用编号存在（至少一条）、被引编号全部在本次证据槽内。
模型接口的请求成功不等于答案事实正确——语义一致性靠评测与抽样复核，
本模块只做结构性校验。失败由调用方重生成一次（§8.3）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

CITATION_RE = re.compile(r"\[(S\d+)\]")


@dataclass
class ValidationResult:
    ok: bool
    error_code: str | None = None  # empty_answer | missing_citation | invalid_citation
    invalid_sources: list[str] = field(default_factory=list)


def validate_answer(answer: str | None, valid_source_ids: set[str]) -> ValidationResult:
    if not answer or not answer.strip():
        return ValidationResult(ok=False, error_code="empty_answer")
    cited = set(CITATION_RE.findall(answer))
    if not cited:
        return ValidationResult(ok=False, error_code="missing_citation")
    invalid = sorted(cited - set(valid_source_ids))
    if invalid:
        return ValidationResult(ok=False, error_code="invalid_citation", invalid_sources=invalid)
    return ValidationResult(ok=True)
