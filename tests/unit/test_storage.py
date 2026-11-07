"""MinIO 存储隔离单测：只使用本地 fake，不读 .env、不连接任何服务。"""

import io
import threading
from datetime import date
from unittest.mock import Mock
from urllib.parse import urlsplit

import minio
import pytest
import urllib3
from minio.error import S3Error
from urllib3.exceptions import ConnectTimeoutError, ProtocolError, ReadTimeoutError
from urllib3.response import HTTPResponse

import app.storage as storage_module
from app.core.config import Settings
from app.storage import (
    MinioStorage,
    StorageConfigurationError,
    StorageError,
    StorageNotFound,
    build_object_key,
    object_uri,
    parse_object_uri,
)

DAY = date(2026, 11, 7)


@pytest.fixture(autouse=True)
def prohibit_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("单元测试不得连接网络")

    monkeypatch.setattr("socket.socket.connect", forbidden)
    monkeypatch.setattr("socket.socket.connect_ex", forbidden)
    monkeypatch.setattr("socket.create_connection", forbidden)


@pytest.fixture
def settings():
    return Settings(
        _env_file=None,
        minio_endpoint="127.0.0.1:9000",
        minio_bucket="nplrag",
        minio_access_key="amdin",
        minio_secret_key="unit-test-secret",
        minio_secure=False,
    )


class Response:
    def __init__(self, payload=b"data", error=None, close_error=None):
        self.payload = payload
        self.error = error
        self.close_error = close_error
        self.closed = False
        self.released = False

    def read(self):
        if self.error:
            raise self.error
        return self.payload

    def close(self):
        self.closed = True
        if self.close_error:
            raise self.close_error

    def release_conn(self):
        self.released = True


class FakeMinio:
    def __init__(self):
        self.objects = {}
        self.last_response = None

    def put_object(self, bucket, key, data, length, *, content_type, part_size):
        payload = data.read()
        assert length == len(payload)
        assert part_size >= max(length, 5 * 1024 * 1024)
        self.objects[(bucket, key)] = (payload, content_type)

    def get_object(self, bucket, key):
        if (bucket, key) not in self.objects:
            raise s3_error("NoSuchKey")
        self.last_response = Response(self.objects[(bucket, key)][0])
        return self.last_response

    def remove_object(self, bucket, key):
        self.objects.pop((bucket, key), None)


def s3_error(code):
    return S3Error(None, code, "sensitive-server-body", None, None, None)


def test_default_settings_without_dotenv(monkeypatch):
    for name in (
        "MINIO_ENDPOINT", "MINIO_BUCKET", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "MINIO_SECURE"
    ):
        monkeypatch.delenv(name, raising=False)
    actual = Settings(_env_file=None)
    assert (actual.minio_endpoint, actual.minio_bucket, actual.minio_access_key) == (
        "127.0.0.1:9000", "nlprag", "admin"
    )
    assert not actual.minio_secure
    assert actual.minio_secret_key == ""


def test_client_configuration_and_secret_is_not_in_repr(settings):
    storage = MinioStorage(settings)
    assert type(storage._client) is storage_module._NoRetryMinio
    assert type(storage._client._http) is storage_module._NoRetryPoolManager
    assert storage._client._http.connection_pool_kw["retries"].total is False
    assert storage.safe_mutation_no_retry is True
    assert "unit-test-secret" not in repr(settings)


def test_untrusted_injected_client_never_claims_no_retry(settings):
    client = FakeMinio()
    client.safe_mutation_no_retry = True
    storage = MinioStorage(settings, client=client)
    assert storage.safe_mutation_no_retry is False
    with pytest.raises(AttributeError):
        storage.safe_mutation_no_retry = True


@pytest.mark.parametrize("module", [minio, urllib3])
def test_unaudited_version_fails_closed_before_mutation(settings, monkeypatch, module):
    storage = MinioStorage(settings)
    monkeypatch.setattr(module, "__version__", "999.0.0")
    assert storage.safe_mutation_no_retry is False
    client = storage._client
    put = Mock()
    delete = Mock()
    monkeypatch.setattr(client, "put_object", put)
    monkeypatch.setattr(client, "remove_object", delete)
    key = build_object_key(1, "a.txt", local_date=DAY)
    uri = object_uri("nplrag", key)
    with pytest.raises(StorageConfigurationError):
        storage.put(key, b"content")
    with pytest.raises(StorageConfigurationError):
        storage.delete(uri)
    put.assert_not_called()
    delete.assert_not_called()


def test_replaced_transport_fails_closed(settings, monkeypatch):
    storage = MinioStorage(settings)
    monkeypatch.setattr(storage._client, "_http", urllib3.PoolManager())
    assert storage.safe_mutation_no_retry is False
    with pytest.raises(StorageConfigurationError):
        storage.delete(object_uri("nplrag", build_object_key(1, "a.txt", local_date=DAY)))


def test_missing_credential_rejected_without_constructing_client(settings, monkeypatch):
    factory = Mock()
    monkeypatch.setattr("app.storage._NoRetryMinio", factory)
    settings.minio_secret_key = ""
    with pytest.raises(StorageConfigurationError):
        MinioStorage(settings)
    factory.assert_not_called()


def test_bad_bucket_rejected_before_operations(settings):
    settings.minio_bucket = "INVALID/bucket"
    with pytest.raises(StorageConfigurationError):
        MinioStorage(settings, client=FakeMinio())


def test_expected_object_key_and_local_date(monkeypatch):
    assert build_object_key(123, "报告.pdf", local_date=DAY) == "2026/11/07/123/报告/报告.pdf"

    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 11, 7)

    monkeypatch.setattr("app.storage.date", FixedDate)
    assert build_object_key(123, "报告.pdf") == "2026/11/07/123/报告/报告.pdf"


@pytest.mark.parametrize(
    "filename",
    ["", ".", "..", "../a.txt", "a/b.txt", "a\\b.txt", "a\x00.txt", "a\n.txt", "\ud800.txt"],
)
def test_reject_invalid_filename(filename):
    with pytest.raises(ValueError):
        build_object_key(1, filename, local_date=DAY)


@pytest.mark.parametrize("user", [0, -1, True, "123", 1.5])
def test_reject_invalid_user_id(user):
    with pytest.raises(ValueError):
        build_object_key(user, "报告.pdf", local_date=DAY)


@pytest.mark.parametrize("filename", [
    "报告.pdf", "公司 2026 (一).tar.gz", "百分%问?井#加+&@!.txt", ".hidden", "a", "a..txt"
])
def test_unicode_and_special_characters_round_trip(settings, filename):
    client = FakeMinio()
    storage = MinioStorage(settings, client=client)
    key = build_object_key(123, filename, local_date=DAY)
    uri = storage.put(key, b"\x00\xffbytes", content_type="application/test")
    assert uri.startswith("s3://nplrag/")
    assert "amdin" not in uri
    assert "unit-test-secret" not in uri
    assert parse_object_uri(uri, bucket="nplrag") == key
    assert storage.get(uri) == b"\x00\xffbytes"
    assert client.last_response.closed and client.last_response.released
    assert client.objects[("nplrag", key)][1] == "application/test"
    assert storage.delete(uri) is None
    assert storage.delete(uri) is None
    with pytest.raises(StorageNotFound):
        storage.get(uri)


def test_official_sdk_through_fake_http_transport(settings, monkeypatch):
    """执行真实 SDK 编码和签名路径，但替换传输层并禁止任何 socket 连接。"""
    objects = {}
    requests = []

    def fake_urlopen(
        self, method, url, *, body=None, headers=None, preload_content=True,
        retries=None, redirect=True,
    ):
        assert retries is False and redirect is False
        parsed = urlsplit(url)
        requests.append((method, parsed.path, parsed.query))
        if parsed.query == "location=":
            xml = b'<LocationConstraint xmlns="http://s3.amazonaws.com/doc/2006-03-01/"/>'
            return HTTPResponse(body=io.BytesIO(xml), status=200)
        if method == "PUT":
            objects[parsed.path] = body
            return HTTPResponse(status=200, headers={"etag": '"fake-etag"'})
        if method == "GET" and parsed.path in objects:
            return HTTPResponse(
                body=io.BytesIO(objects[parsed.path]), status=200, preload_content=False
            )
        if method == "GET":
            xml = b"<Error><Code>NoSuchKey</Code><Message>not found</Message></Error>"
            return HTTPResponse(
                body=io.BytesIO(xml), status=404, headers={"content-type": "application/xml"},
                preload_content=False,
            )
        if method == "DELETE":
            objects.pop(parsed.path, None)
            return HTTPResponse(status=204)
        raise AssertionError(f"unexpected request method {method}")

    monkeypatch.setattr("urllib3.PoolManager.urlopen", fake_urlopen)
    storage = MinioStorage(settings)
    key = build_object_key(123, "报告 %?#.txt", local_date=DAY)
    uri = storage.put(key, b"payload")
    assert storage.get(uri) == b"payload"
    storage.delete(uri)
    with pytest.raises(StorageNotFound):
        storage.get(uri)
    assert requests[1][1] == urlsplit(uri).path.replace("/2026/", "/nplrag/2026/", 1)
    assert not objects


def _prepare_real_sdk_with_fake_connection(settings, monkeypatch, make_request):
    """保留真实 urllib3 pool/retry/redirect 路径，仅替换最低层发送。"""
    monkeypatch.setattr("urllib3.connectionpool.HTTPConnectionPool._make_request", make_request)
    storage = MinioStorage(settings)
    assert storage.safe_mutation_no_retry is True
    # 区域读取是 GET，与目标对象写删无关；其他测试覆盖 location 读取。
    storage._client._region_map["nplrag"] = "us-east-1"
    key = build_object_key(1, "报告.txt", local_date=DAY)
    return storage, key, object_uri("nplrag", key)


@pytest.mark.parametrize("operation", ["put", "delete"])
@pytest.mark.parametrize("fault", ["read_timeout", "connect_timeout", "protocol", "oserror"])
def test_mutations_do_not_retry_transport_errors(settings, monkeypatch, operation, fault):
    calls = []

    def make_request(self, conn, method, url, **kwargs):
        calls.append((method, url))
        if len(calls) > 1:
            return HTTPResponse(status=200 if method == "PUT" else 204)
        if fault == "read_timeout":
            raise ReadTimeoutError(self, url, "ambiguous timeout")
        if fault == "connect_timeout":
            raise ConnectTimeoutError("connection timeout")
        if fault == "protocol":
            raise ProtocolError("ambiguous connection reset")
        raise OSError("connection failure")

    storage, key, uri = _prepare_real_sdk_with_fake_connection(settings, monkeypatch, make_request)
    with pytest.raises(StorageError, match="不确定"):
        if operation == "put":
            storage.put(key, b"content")
        else:
            storage.delete(uri)
    assert [method for method, _ in calls] == ["PUT" if operation == "put" else "DELETE"]


@pytest.mark.parametrize("operation", ["put", "delete"])
@pytest.mark.parametrize(
    "fault", ["500", "503-retry-after", "429-retry-after", "301", "302", "303", "307", "308",
              "retryhead"]
)
def test_mutations_do_not_retry_status_redirect_or_sdk_retryhead(
    settings, monkeypatch, operation, fault
):
    calls = []

    def make_request(self, conn, method, url, **kwargs):
        calls.append((method, url))
        if len(calls) > 1:
            return HTTPResponse(status=200 if method == "PUT" else 204)
        status = 400 if fault == "retryhead" else int(fault.split("-")[0])
        headers = {"content-type": "application/xml"}
        if status in (301, 302, 303, 307, 308):
            headers["location"] = "http://unreachable.invalid/redirected"
        if status in (429, 503):
            headers["retry-after"] = "0"
        code = "RetryHead" if fault == "retryhead" else "ServerError"
        body = f"<Error><Code>{code}</Code><Message>error</Message></Error>".encode()
        return HTTPResponse(body=io.BytesIO(body), status=status, headers=headers)

    storage, key, uri = _prepare_real_sdk_with_fake_connection(settings, monkeypatch, make_request)
    with pytest.raises(StorageError, match="不确定"):
        if operation == "put":
            storage.put(key, b"content")
        else:
            storage.delete(uri)
    assert [method for method, _ in calls] == ["PUT" if operation == "put" else "DELETE"]


@pytest.mark.parametrize("status", [404, 500, 503])
def test_delete_missing_code_does_not_mask_server_failure(settings, monkeypatch, status):
    calls = []

    def make_request(self, conn, method, url, **kwargs):
        calls.append(method)
        body = b"<Error><Code>NoSuchKey</Code><Message>missing</Message></Error>"
        return HTTPResponse(
            body=io.BytesIO(body), status=status, headers={"content-type": "application/xml"}
        )

    storage, _, uri = _prepare_real_sdk_with_fake_connection(settings, monkeypatch, make_request)
    if status == 404:
        assert storage.delete(uri) is None
    else:
        with pytest.raises(StorageError, match="不确定"):
            storage.delete(uri)
    assert calls == ["DELETE"]


@pytest.mark.parametrize("size", [0, 5 * 1024 * 1024, 5 * 1024 * 1024 + 1])
def test_large_write_and_delete_are_each_one_wire_request(settings, monkeypatch, size):
    calls = []

    def make_request(self, conn, method, url, **kwargs):
        calls.append((method, url, len(kwargs.get("body") or b"")))
        return HTTPResponse(status=200 if method == "PUT" else 204, headers={"etag": '"etag"'})

    storage, key, uri = _prepare_real_sdk_with_fake_connection(settings, monkeypatch, make_request)
    payload = b"a" * size
    assert storage.put(key, payload) == uri
    storage.delete(uri)
    assert [(method, length) for method, _, length in calls] == [
        ("PUT", len(payload)), ("DELETE", 0)
    ]
    assert all("uploadId=" not in url and "uploads=" not in url for _, url, _ in calls)


def test_oversize_object_rejected_before_any_request(settings, monkeypatch):
    storage = MinioStorage(settings)
    monkeypatch.setattr(storage_module, "_MAX_SINGLE_PUT_SIZE", 3)
    put = Mock()
    monkeypatch.setattr(storage._client, "put_object", put)
    key = build_object_key(1, "a.txt", local_date=DAY)
    with pytest.raises(ValueError, match="PUT"):
        storage.put(key, b"four")
    put.assert_not_called()


def test_pool_forces_no_retries_even_against_call_overrides(monkeypatch):
    captured = []

    def urlopen(self, method, url, **kwargs):
        captured.append(kwargs)
        return HTTPResponse(status=200)

    monkeypatch.setattr("urllib3.PoolManager.urlopen", urlopen)
    pool = storage_module._NoRetryPoolManager()
    pool.urlopen("PUT", "http://unused.invalid/test", retries=5, redirect=True)
    assert captured[0]["retries"] is False
    assert captured[0]["redirect"] is False
    assert pool.connection_pool_kw["cert_reqs"] == "CERT_REQUIRED"


def test_in_flight_replace_needs_caller_coordination(settings):
    """较早请求可在较晚请求返回之后落盘，存储层不承诺跨请求的顺序。"""
    first_started = threading.Event()
    release_first = threading.Event()

    class DelayedMinio(FakeMinio):
        def put_object(self, bucket, key, data, length, *, content_type, part_size):
            payload = data.read()
            if payload == b"first":
                first_started.set()
                if not release_first.wait(timeout=5):
                    raise TimeoutError("test synchronization failed")
            super().put_object(
                bucket, key, io.BytesIO(payload), length, content_type=content_type,
                part_size=part_size,
            )

    storage = MinioStorage(settings, client=DelayedMinio())
    key = build_object_key(1, "name.txt", local_date=DAY)
    first_result = []
    first_error = []

    def first_put():
        try:
            first_result.append(storage.put(key, b"first"))
        except Exception as exc:
            first_error.append(exc)

    thread = threading.Thread(target=first_put)
    thread.start()
    try:
        assert first_started.wait(timeout=5)
        uri = storage.put(key, b"second")
        assert storage.get(uri) == b"second"
    finally:
        release_first.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert not first_error
    assert first_result == [uri]
    assert storage.get(uri) == b"first"


def test_same_key_put_replaces_object(settings):
    storage = MinioStorage(settings, client=FakeMinio())
    key = build_object_key(1, "name.txt", local_date=DAY)
    first = storage.put(key, b"first")
    second = storage.put(key, b"second")
    assert first == second
    assert storage.get(first) == b"second"


@pytest.mark.parametrize("uri", [
    "uploads/old.txt", "s3://other/2026/11/07/1/a/a.txt", "s3://user:password@nplrag/x",
    "s3://nplrag/2026/11/07/1/a/a.txt?x=1", "s3://nplrag/2026/11/07/1/a/a.txt#frag",
    "s3://nplrag/2026/11/07/1/a/a%2Ftxt", "s3://nplrag/2026/11/07/1/a/a%GGtxt",
    "s3://nplrag/2026/11/07/1/a/a%FFtxt", "s3://nplrag/2026/11/07/1/a/wrong.txt",
    "s3://nplrag/2026/02/30/1/a/a.txt", "s3://nplrag/2026/11/07/01/a/a.txt",
])
def test_reject_invalid_uri_before_calls(settings, uri):
    client = Mock()
    storage = MinioStorage(settings, client=client)
    with pytest.raises(ValueError):
        storage.get(uri)
    with pytest.raises(ValueError):
        storage.delete(uri)
    client.assert_not_called()
    assert not client.method_calls


def test_uri_length_is_checked_before_put(settings):
    client = Mock()
    storage = MinioStorage(settings, client=client)
    # A valid key may still be too long for varchar(1000) after URI escaping.
    key = build_object_key(1, "文" * 70 + ".txt", local_date=DAY)
    with pytest.raises(ValueError, match="URI"):
        storage.put(key, b"data")
    assert not client.method_calls


def test_key_length_is_checked():
    with pytest.raises(ValueError, match="key"):
        build_object_key(1, "a" * 510 + ".txt", local_date=DAY)


def test_sdk_constructor_error_does_not_expose_detail(settings, monkeypatch):
    factory = Mock(side_effect=ValueError("unit-test-secret"))
    monkeypatch.setattr("app.storage._NoRetryMinio", factory)
    with pytest.raises(StorageConfigurationError) as exc:
        MinioStorage(settings)
    assert "unit-test-secret" not in str(exc.value)


@pytest.mark.parametrize("method", ["put_object", "get_object", "remove_object"])
def test_transport_errors_are_not_reported_as_success(settings, method):
    client = Mock()
    getattr(client, method).side_effect = TimeoutError("details")
    storage = MinioStorage(settings, client=client)
    key = build_object_key(1, "a.txt", local_date=DAY)
    uri = object_uri("nplrag", key)
    operations = {
        "put_object": lambda: storage.put(key, b"x"),
        "get_object": lambda: storage.get(uri),
        "remove_object": lambda: storage.delete(uri),
    }
    with pytest.raises(StorageError) as exc:
        operations[method]()
    assert "details" not in str(exc.value)
    if method != "get_object":
        assert "不确定" in str(exc.value)


def test_errors_are_wrapped_and_do_not_expose_server_detail(settings):
    client = Mock()
    storage = MinioStorage(settings, client=client)
    key = build_object_key(1, "a.txt", local_date=DAY)
    uri = object_uri("nplrag", key)
    client.put_object.side_effect = s3_error("AccessDenied")
    client.get_object.side_effect = s3_error("AccessDenied")
    client.remove_object.side_effect = s3_error("AccessDenied")
    operations = (
        lambda: storage.put(key, b"x"),
        lambda: storage.get(uri),
        lambda: storage.delete(uri),
    )
    for operation in operations:
        with pytest.raises(StorageError) as exc:
            operation()
        assert "sensitive-server-body" not in str(exc.value)
        assert "unit-test-secret" not in str(exc.value)
    client.get_object.side_effect = s3_error("NoSuchBucket")
    with pytest.raises(StorageError):
        storage.get(uri)


@pytest.mark.parametrize(
    "read_error,close_error", [(OSError("broken read"), None), (None, OSError("broken close"))]
)
def test_response_is_released_even_on_failure(settings, read_error, close_error):
    response = Response(error=read_error, close_error=close_error)
    client = Mock()
    client.get_object.return_value = response
    storage = MinioStorage(settings, client=client)
    uri = object_uri("nplrag", build_object_key(1, "a.txt", local_date=DAY))
    with pytest.raises(StorageError):
        storage.get(uri)
    assert response.closed and response.released
