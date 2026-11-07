"""内存对象存储：测试期间不构造 SDK 客户端，也不访问网络或磁盘。"""

from __future__ import annotations

import hashlib
import io
from threading import RLock

from minio.error import S3Error

from app.storage import MinioStorage, build_object_key, parse_object_uri

TEST_BUCKET = "nlp-rag-isolated-test"


class _Response(io.BytesIO):
    def release_conn(self):
        pass


class FakeMinio:
    """按 SDK 的 PUT/GET/DELETE 接口实现，每个测试拥有独立实例。"""

    def __init__(self):
        self.objects: dict[tuple[str, str], bytes] = {}
        self.events: list[tuple[str, str]] = []
        self._lock = RLock()

    def _check_bucket(self, bucket: str) -> None:
        if bucket != TEST_BUCKET:
            raise AssertionError("测试只允许使用隔离的 fake bucket")

    def put_object(self, bucket, key, data, length, *, content_type, part_size=0):
        self._check_bucket(bucket)
        payload = data.read()
        if len(payload) != length:
            raise ValueError("对象长度不匹配")
        with self._lock:
            self.events.append(("put", key))
            self.objects[(bucket, key)] = payload

    def get_object(self, bucket, key):
        self._check_bucket(bucket)
        with self._lock:
            self.events.append(("get", key))
            if (bucket, key) not in self.objects:
                raise S3Error(None, "NoSuchKey", "not found", None, None, None)
            return _Response(self.objects[(bucket, key)])

    def remove_object(self, bucket, key):
        self._check_bucket(bucket)
        with self._lock:
            self.events.append(("delete", key))
            self.objects.pop((bucket, key), None)

    def storage(self, settings):
        if settings.minio_bucket != TEST_BUCKET:
            raise AssertionError("测试配置必须使用隔离的 fake bucket")
        return _FakeStorage(settings, client=self)

    def seed(self, settings, filename: str, payload: bytes, *, user_id: int = 1):
        """写入符合生产 URI 规则的测试对象并返回 URI 和二进制摘要。"""
        key = build_object_key(user_id, filename)
        uri = self.storage(settings).put(key, payload)
        return uri, hashlib.sha256(payload).digest()

    def contains(self, uri: str) -> bool:
        key = parse_object_uri(uri, bucket=TEST_BUCKET)
        with self._lock:
            return (TEST_BUCKET, key) in self.objects


class _FakeStorage(MinioStorage):
    # 纯内存操作同步完成，不会在返回后重试或迟到。
    safe_mutation_no_retry = True
