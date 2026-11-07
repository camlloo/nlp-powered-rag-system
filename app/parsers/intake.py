"""上传入口：三重校验、大小限制、随机内部名存储与摘要（实现文档 §4.1）。

原文件以随机内部名称保存，不直接信任客户端路径；
解析超时（120s）由 worker 层执行，本模块只做同步解析。
"""

from __future__ import annotations

import hashlib
import secrets
from pathlib import Path, PurePosixPath

from app.core.config import Settings, get_settings
from app.parsers.base import check_size, detect_format
from app.parsers.docx import parse_docx
from app.parsers.markdown import parse_markdown
from app.parsers.pdf import parse_pdf
from app.parsers.txt import parse_txt
from app.schemas.contracts import DocFormat, ParsedDocument

DEFAULT_UPLOAD_DIR = Path("data/uploads")

_EXT_BY_FORMAT: dict[DocFormat, str] = {
    "txt": ".txt",
    "markdown": ".md",
    "docx": ".docx",
    "pdf": ".pdf",
}


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


def save_upload(
    payload: bytes, fmt: DocFormat, upload_dir: Path | None = None
) -> tuple[str, str]:
    """随机内部名保存原文件，返回 (storage_uri, sha256_hex)。

    uri 形如 uploads/<随机名>.<ext>；数据库保存路径与摘要，文件本体不入库。
    """
    directory = upload_dir or DEFAULT_UPLOAD_DIR
    directory.mkdir(parents=True, exist_ok=True)
    name = f"{secrets.token_hex(16)}{_EXT_BY_FORMAT[fmt]}"
    (directory / name).write_bytes(payload)
    return f"uploads/{name}", hashlib.sha256(payload).hexdigest()
