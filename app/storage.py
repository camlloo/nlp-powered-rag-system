"""MinIO 原文件存储；固定 key 的覆盖与数据库事务由调用方协调。

同名 PUT 是覆盖而不是原子条件创建。不能在请求失败后盲目删除 key，
否则可能删除另一个并发请求已经提交的对象；也不能用本模块恢复旧内容。
"""

from __future__ import annotations

import io
import os
import re
from datetime import date
from pathlib import PurePosixPath
from urllib.parse import quote, unquote_to_bytes, urlsplit

import certifi
import minio
import urllib3
from minio import Minio
from minio.error import S3Error
from urllib3 import PoolManager
from urllib3.util import Timeout

from app.core.config import Settings

_MAX_KEY_BYTES = 1024
# kb_document_version.storage_uri 是 varchar(1000)。
_MAX_URI_CHARS = 1000
_BUCKET = re.compile(r"^(?!\d+\.\d+\.\d+\.\d+$)[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_AUDITED_MINIO_VERSION = "7.2.20"
_AUDITED_URLLIB3_VERSION = "2.8.0"
_MIN_PART_SIZE = 5 * 1024 * 1024
_MAX_SINGLE_PUT_SIZE = 5 * 1024 * 1024 * 1024


class _NoRetryPoolManager(PoolManager):
    """每个请求都强制禁用 urllib3 重试及 PoolManager 的跨主机重定向。"""

    def __init__(self) -> None:
        super().__init__(
            timeout=Timeout(connect=300, read=300),
            maxsize=10,
            cert_reqs="CERT_REQUIRED",
            ca_certs=os.environ.get("SSL_CERT_FILE") or certifi.where(),
            retries=False,
        )

    def urlopen(self, method, url, **kwargs):
        # PoolManager 默认 redirect=True；即便连接池禁重试，也必须显式禁止重定向。
        kwargs["retries"] = False
        kwargs["redirect"] = False
        return super().urlopen(method, url, **kwargs)


class _NoRetryMinio(Minio):
    """跳过 7.2.20 的通用 RetryHead 重试分支，包括服务端返回的同名错误码。"""

    def _execute(
        self, method, bucket_name=None, object_name=None, body=None, headers=None,
        query_params=None, preload_content=True, no_body_trace=False,
    ):
        region = self._get_region(bucket_name)
        return self._url_open(
            method,
            region,
            bucket_name=bucket_name,
            object_name=object_name,
            body=body,
            headers=headers,
            query_params=query_params,
            preload_content=preload_content,
            no_body_trace=no_body_trace,
        )


class StorageError(OSError):
    """对象存储请求失败；消息不包含 SDK 返回内容或凭据。"""


class StorageConfigurationError(StorageError):
    """对象存储配置无效或缺少凭据。"""


class StorageNotFound(FileNotFoundError):
    """指定的对象不存在。"""


def _validate_bucket(bucket: str) -> None:
    if not _BUCKET.fullmatch(bucket) or ".." in bucket or ".-" in bucket or "-." in bucket:
        raise ValueError("非法 MinIO bucket 名称")


def _validate_filename(filename: str) -> str:
    if not isinstance(filename, str) or not filename or filename in {".", ".."}:
        raise ValueError("非法文件名")
    if "/" in filename or "\\" in filename or any(ord(c) < 32 or ord(c) == 127 for c in filename):
        raise ValueError("非法文件名")
    try:
        filename.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("文件名不是有效的 UTF-8") from None
    # PurePosixPath.stem 仅移除最后一个扩展名，与原有解析入口保持一致。
    stem = PurePosixPath(filename).stem
    if not stem or stem in {".", ".."}:
        raise ValueError("非法文件名")
    return stem


def build_object_key(user_id: int, filename: str, *, local_date: date | None = None) -> str:
    """以本地日期和原始文件名构造 key；非法输入直接拒绝，不做重命名。"""
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        raise ValueError("用户 ID 必须是正整数")
    if local_date is None:
        local_date = date.today()
    if not isinstance(local_date, date):
        raise ValueError("日期无效")
    stem = _validate_filename(filename)
    key = (
        f"{local_date.year:04d}/{local_date.month:02d}/{local_date.day:02d}"
        f"/{user_id}/{stem}/{filename}"
    )
    if len(key.encode("utf-8")) > _MAX_KEY_BYTES:
        raise ValueError("对象 key 超过 MinIO 长度限制")
    return key


def _validate_key(key: str) -> None:
    if not isinstance(key, str):
        raise ValueError("非法对象 key")
    parts = key.split("/")
    if len(parts) != 6:
        raise ValueError("非法对象 key")
    year, month, day, user, _, filename = parts
    if not (re.fullmatch(r"[0-9]{4}", year) and re.fullmatch(r"[0-9]{2}", month)
            and re.fullmatch(r"[0-9]{2}", day) and re.fullmatch(r"[1-9][0-9]*", user)):
        raise ValueError("非法对象 key")
    try:
        parsed_date = date(int(year), int(month), int(day))
        expected = build_object_key(int(user), filename, local_date=parsed_date)
    except (OverflowError, ValueError):
        raise ValueError("非法对象 key") from None
    if key != expected:
        raise ValueError("非法对象 key")


def object_uri(bucket: str, key: str) -> str:
    """生成无凭据、可无损解码的 s3 URI。"""
    _validate_bucket(bucket)
    _validate_key(key)
    uri = f"s3://{bucket}/{quote(key, safe='/')}"
    if len(uri) > _MAX_URI_CHARS:
        raise ValueError("对象 URI 超过数据库字段长度限制")
    return uri


def parse_object_uri(uri: str, *, bucket: str) -> str:
    """严格校验 URI 与桶，并返回未解码损失的原始对象 key。"""
    _validate_bucket(bucket)
    if not isinstance(uri, str) or len(uri) > _MAX_URI_CHARS:
        raise ValueError("非法对象 URI")
    parsed = urlsplit(uri)
    if (parsed.scheme != "s3" or parsed.netloc != bucket or not parsed.path.startswith("/")
            or parsed.query or parsed.fragment or _PERCENT_ESCAPE.search(parsed.path)):
        raise ValueError("非法对象 URI")
    try:
        key = unquote_to_bytes(parsed.path[1:]).decode("utf-8", "strict")
        # 只接受本模块生成的规范形式，拒绝隐藏的分隔符及畸形编码。
        if object_uri(bucket, key) != uri:
            raise ValueError("非法对象 URI")
    except (UnicodeDecodeError, UnicodeEncodeError, ValueError):
        raise ValueError("非法对象 URI") from None
    return key


class MinioStorage:
    """对固定桶提供同步 PUT/GET/DELETE，不自动创建桶，也不连接本地文件系统。"""

    def __init__(self, settings: Settings, *, client: Minio | None = None) -> None:
        try:
            _validate_bucket(settings.minio_bucket)
        except ValueError:
            raise StorageConfigurationError("MinIO bucket 配置无效") from None
        self.bucket = settings.minio_bucket
        self._owned_pool = None
        if client is not None:
            # 外部注入的实现无法验证，不对其宣称任何无重试保证。
            self._client = client
            self._owns_client = False
            return
        self._owns_client = True
        if not settings.minio_access_key or not settings.minio_secret_key:
            raise StorageConfigurationError("MinIO 访问凭据未配置")
        try:
            self._owned_pool = _NoRetryPoolManager()
            self._client = _NoRetryMinio(
                settings.minio_endpoint,
                access_key=settings.minio_access_key,
                secret_key=settings.minio_secret_key,
                secure=settings.minio_secure,
                http_client=self._owned_pool,
            )
        except Exception:
            raise StorageConfigurationError("MinIO 客户端配置无效") from None

    @property
    def safe_mutation_no_retry(self) -> bool:
        """仅证明当前受控客户端的单次写删不会在本进程内自动重试或跟随重定向。"""
        return (
            self._owns_client
            and minio.__version__ == _AUDITED_MINIO_VERSION
            and urllib3.__version__ == _AUDITED_URLLIB3_VERSION
            and type(self._client) is _NoRetryMinio
            and type(self._owned_pool) is _NoRetryPoolManager
            and self._client._http is self._owned_pool
        )

    def _require_safe_owned_client(self) -> None:
        # 版本变化或自有客户端被替换后，清理等未检查标志的调用也必须 fail closed。
        if self._owns_client and not self.safe_mutation_no_retry:
            raise StorageConfigurationError("MinIO SDK 或传输未通过无重试校验")

    def put(
        self, key: str, payload: bytes, *, content_type: str = "application/octet-stream"
    ) -> str:
        """覆盖写入对象并返回 URI；异常时对象可能已写入，需调用方协调。"""
        uri = object_uri(self.bucket, key)
        if not isinstance(payload, bytes):
            raise ValueError("对象内容必须为 bytes")
        if len(payload) > _MAX_SINGLE_PUT_SIZE:
            raise ValueError("对象超过单次 PUT 大小限制")
        self._require_safe_owned_client()
        try:
            # 固定一个 part，避免 SDK 默认 5 MiB 分片、并行任务及完成/中止请求。
            self._client.put_object(
                self.bucket, key, io.BytesIO(payload), len(payload), content_type=content_type,
                part_size=max(_MIN_PART_SIZE, len(payload)),
            )
        except Exception:
            raise StorageError("MinIO 对象写入失败；结果可能不确定") from None
        return uri

    def get(self, uri: str) -> bytes:
        """读取完整对象；无论读取成功或失败均关闭并释放响应连接。"""
        key = parse_object_uri(uri, bucket=self.bucket)
        try:
            response = self._client.get_object(self.bucket, key)
            try:
                return response.read()
            finally:
                try:
                    response.close()
                finally:
                    response.release_conn()
        except S3Error as exc:
            if exc.code in {"NoSuchKey", "NoSuchObject", "NoSuchVersion"}:
                raise StorageNotFound("MinIO 对象不存在") from None
            raise StorageError("MinIO 对象读取失败") from None
        except Exception:
            raise StorageError("MinIO 对象读取失败") from None

    def delete(self, uri: str) -> None:
        """幂等删除对象。S3 不报告目标此前是否存在，故不返回存在性布尔值。"""
        key = parse_object_uri(uri, bucket=self.bucket)
        self._require_safe_owned_client()
        try:
            self._client.remove_object(self.bucket, key)
        except S3Error as exc:
            if (exc.code in {"NoSuchKey", "NoSuchObject", "NoSuchVersion"}
                    and getattr(exc.response, "status", None) == 404):
                return
            # 不能把 5xx 或无状态的错误码当作删除成功；远端结果仍不确定。
            raise StorageError("MinIO 对象删除失败；结果可能不确定") from None
        except Exception:
            raise StorageError("MinIO 对象删除失败；结果可能不确定") from None
