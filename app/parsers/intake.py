"""上传解析入口：三重校验、大小限制与对象存储读取。

对象删除只允许经由 app.indexing.cleanup 的写入屏障协议执行，
本模块不提供绕过屏障的直接删除接口。
解析超时（120s）由 worker 层执行，本模块只做同步解析。
"""

from __future__ import annotations

from pathlib import PurePosixPath

from app.core.config import Settings, get_settings
from app.parsers.base import check_size, detect_format
from app.parsers.docx import parse_docx
from app.parsers.markdown import parse_markdown
from app.parsers.pdf import parse_pdf
from app.parsers.txt import parse_txt
from app.schemas.contracts import ParsedDocument
from app.storage import MinioStorage


def parse_upload(
    filename: str,
    content_type: str | None,
    payload: bytes,
    settings: Settings | None = None,
) -> ParsedDocument:
    settings = settings or get_settings()
    check_size(len(payload), settings)
    fmt = detect_format(filename, content_type, payload)
    title = PurePosixPath(filename).stem or "未命名文档"
    if fmt == "txt":
        return parse_txt(payload, title, settings)
    if fmt == "markdown":
        return parse_markdown(payload, title, settings)
    if fmt == "docx":
        return parse_docx(payload, title, settings)
    return parse_pdf(payload, title, settings)


def load_upload(storage_uri: str, settings: Settings) -> bytes:
    """读取对象；旧本地路径不会被当作存储回退。"""
    return MinioStorage(settings).get(storage_uri)
