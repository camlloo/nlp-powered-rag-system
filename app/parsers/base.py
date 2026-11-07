"""解析器公共基础：格式识别（扩展名+MIME+文件签名三重校验）与错误类型。

实现文档 §4.1：上传采用扩展名、MIME 类型和文件签名组合校验；
解析限制（大小、页数、压缩展开）在此统一执行。
"""

from __future__ import annotations

import io
import zipfile
from pathlib import PurePosixPath

from app.core.config import Settings
from app.core.errors import PayloadTooLarge, UnsupportedFormat
from app.schemas.contracts import DocFormat

# 错误类型由 app.core.errors 统一定义（即 API 错误面）：
# UnsupportedFormat -> 422 unsupported_format
# PayloadTooLarge   -> 413 payload_too_large
# NeedsReview       -> 422 needs_review

PDF_MAGIC = b"%PDF-"
ZIP_MAGIC = b"PK\x03\x04"

# 扩展名 -> 候选格式
EXT_FORMATS: dict[str, DocFormat] = {
    ".txt": "txt",
    ".md": "markdown",
    ".markdown": "markdown",
    ".docx": "docx",
    ".pdf": "pdf",
}

# 常见 MIME -> 候选格式（宽松匹配：未知 MIME 时以扩展名+签名为准）
MIME_FORMATS: dict[str, DocFormat] = {
    "text/plain": "txt",
    "text/markdown": "markdown",
    "text/x-markdown": "markdown",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/pdf": "pdf",
    "application/octet-stream": None,  # 浏览器兜底类型，不据此判格式
}


def detect_format(filename: str, content_type: str | None, payload: bytes) -> DocFormat:
    """扩展名 + MIME + 文件签名三重校验，任一明确冲突即拒绝。"""
    ext = PurePosixPath(filename.lower()).suffix
    ext_fmt = EXT_FORMATS.get(ext)
    if ext_fmt is None:
        raise UnsupportedFormat(f"不支持的扩展名：{ext or '(无)'}")

    mime_fmt = MIME_FORMATS.get((content_type or "").split(";")[0].strip().lower())
    if mime_fmt is not None and ext_fmt in ("txt", "markdown", "docx", "pdf"):
        # MIME 与扩展名同为已知格式但相互冲突时拒绝（txt/markdown 视作同族文本）
        if {mime_fmt, ext_fmt} - {"txt", "markdown"} and mime_fmt != ext_fmt:
            raise UnsupportedFormat(f"MIME 类型与扩展名不一致：{content_type} vs {ext}")

    head = payload[:8]
    if ext_fmt == "pdf":
        if not head.startswith(PDF_MAGIC):
            raise UnsupportedFormat("文件签名与 PDF 不符")
    elif ext_fmt == "docx":
        if not head.startswith(ZIP_MAGIC):
            raise UnsupportedFormat("文件签名与 DOCX 不符")
        if not _is_docx_zip(payload):
            raise UnsupportedFormat("ZIP 内容不是有效的 DOCX（缺少 word/document.xml）")
    else:
        if head.startswith(PDF_MAGIC) or head.startswith(ZIP_MAGIC):
            raise UnsupportedFormat("文本文件包含可执行文档签名")
    return ext_fmt


def _is_docx_zip(payload: bytes) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            return "word/document.xml" in zf.namelist()
    except zipfile.BadZipFile:
        return False


def check_size(payload_len: int, settings: Settings) -> None:
    if payload_len <= 0:
        raise PayloadTooLarge("空文件")
    if payload_len > settings.max_upload_bytes:
        raise PayloadTooLarge(
            f"文件大小 {payload_len} 超过限制 {settings.max_upload_bytes}"
        )


def check_zip_expansion(payload: bytes, settings: Settings) -> None:
    """校验压缩文件展开大小，防止 DOCX 解压异常（zip 炸弹）。"""
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            total = sum(info.file_size for info in zf.infolist())
    except zipfile.BadZipFile as exc:
        raise UnsupportedFormat("无法读取压缩包") from exc
    if total > settings.docx_zip_guard_bytes:
        raise PayloadTooLarge(
            f"DOCX 解压后大小 {total} 超过限制 {settings.docx_zip_guard_bytes}"
        )


def ensure_page_count(page_count: int, settings: Settings) -> None:
    if page_count > settings.max_pdf_pages:
        raise PayloadTooLarge(f"PDF 页数 {page_count} 超过限制 {settings.max_pdf_pages}")
