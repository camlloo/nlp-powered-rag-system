"""intake 三重校验、大小限制与存储单测。"""

from unittest.mock import Mock

import pytest

from app.core.config import Settings
from app.core.errors import PayloadTooLarge, UnsupportedFormat
from app.parsers import intake
from app.parsers.base import detect_format


def test_detect_pdf_by_magic():
    assert detect_format("a.pdf", "application/pdf", b"%PDF-1.7\n...") == "pdf"


def test_detect_docx_requires_word_xml(tmp_path):
    import io
    import zipfile

    plain_zip = io.BytesIO()
    with zipfile.ZipFile(plain_zip, "w") as zf:
        zf.writestr("readme.txt", "not a docx")
    with pytest.raises(UnsupportedFormat, match="DOCX"):
        detect_format("a.docx", "application/octet-stream", plain_zip.getvalue())


def test_detect_mime_extension_conflict():
    docx_mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    with pytest.raises(UnsupportedFormat, match="不一致"):
        detect_format("a.pdf", docx_mime, b"%PDF-1.4")


def test_detect_text_with_executable_signature_rejected():
    with pytest.raises(UnsupportedFormat):
        detect_format("a.txt", "text/plain", b"%PDF-1.4 fake")


def test_detect_unknown_extension():
    with pytest.raises(UnsupportedFormat, match="扩展名"):
        detect_format("a.exe", None, b"MZ...")


def test_check_size_empty_and_oversize():
    settings = Settings(_env_file=None, max_upload_bytes=10)
    with pytest.raises(PayloadTooLarge, match="空文件"):
        intake.parse_upload("a.txt", "text/plain", b"", settings)
    with pytest.raises(PayloadTooLarge, match="超过限制"):
        intake.parse_upload("a.txt", "text/plain", b"x" * 11, settings)


def test_parse_upload_dispatch_txt():
    parsed = intake.parse_upload(
        "制度.txt", "text/plain", "员工报销".encode(), Settings(_env_file=None)
    )
    assert parsed.fmt == "txt"
    assert parsed.title == "制度"
    assert parsed.encoding == "utf-8"


def test_load_uses_object_storage(monkeypatch):
    storage = Mock()
    storage.get.return_value = b"hello rag"
    factory = Mock(return_value=storage)
    monkeypatch.setattr(intake, "MinioStorage", factory)
    settings = Settings(_env_file=None, minio_bucket="nlprag")
    uri = "s3://nlprag/2026/11/07/1/a/a.txt"

    assert intake.load_upload(uri, settings) == b"hello rag"
    factory.assert_called_once_with(settings)
    storage.get.assert_called_once_with(uri)
    # 直接删除接口已移除：物理删除只能经 cleanup 屏障协议。
    assert not hasattr(intake, "delete_upload")


def test_storage_read_failure_propagates(monkeypatch):
    storage = Mock()
    storage.get.side_effect = OSError("storage unavailable")
    monkeypatch.setattr(intake, "MinioStorage", Mock(return_value=storage))
    with pytest.raises(OSError, match="storage unavailable"):
        intake.load_upload("s3://nlprag/2026/11/07/1/a/a.txt", Settings(_env_file=None))
