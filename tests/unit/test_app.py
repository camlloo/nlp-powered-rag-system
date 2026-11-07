"""服务入口冒烟：不依赖数据库的接口与 request_id 关联。"""

from fastapi.testclient import TestClient

from app.main import app


def test_healthz():
    client = TestClient(app)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert resp.headers.get("x-request-id")


def test_root_info():
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    if "text/html" in resp.headers.get("content-type", ""):
        # 前端构建产物存在时根路径托管检索台
        assert 'id="app"' in resp.text
    else:
        assert resp.json()["docs"] == "/docs"


def test_error_envelope():
    client = TestClient(app)
    resp = client.get("/not-exist")
    assert resp.status_code == 404
    body = resp.json()
    assert {"code", "message", "request_id", "retryable"} <= set(body)
