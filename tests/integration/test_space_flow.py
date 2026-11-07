"""去知识库化产品面回归：组织/个人归属 + 公开/私有的可见性矩阵。

POST/GET /documents 不暴露 kb_id；问答缺省 kb_id 检索主空间；
上传者（无 KB 成员身份）可轮询自己的任务；
PATCH /documents/{id} 修改归属与可见性（块级冗余列同步 + acl_revision 递增）。
"""

import asyncio
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.core.config import get_settings
from app.db.models import (
    AppUser,
    KbChunk,
    KbCollection,
    KbDocumentVersion,
    KbMember,
    OrgTag,
    UserOrgTag,
)
from app.db.session import SessionLocal
from app.main import app
from tests.integration.helpers import cleanup_kb, cleanup_uploaded_documents


@pytest.fixture
def space_env(pg_ready):
    """两名不同组织的用户（甲：技术部；乙：市场部），全部走新接口。"""
    prefix = get_settings().api_prefix
    accounts: dict[str, dict] = {}
    tag_names = {}
    with TestClient(app) as client:
        for key, tag in (("甲", "空间测试-技术部"), ("乙", "空间测试-市场部")):
            tag_names[key] = f"{tag}-{uuid4().hex[:8]}"
            payload = {
                "username": f"space_{uuid4().hex}",
                "password": "test-password",
                "display_name": f"空间-{key}",
                "org_tags": [tag_names[key]],
            }
            resp = client.post(f"{prefix}/auth/register", json=payload)
            assert resp.status_code == 201, resp.text
            accounts[key] = {**resp.json(), "credentials": payload}
        try:
            yield client, prefix, {"accounts": accounts, "tag_names": tag_names}
        finally:
            user_ids = [a["user"]["user_id"] for a in accounts.values()]
            # 上传到主空间的测试文档随夹具清理，避免共享开发库残留
            cleanup_uploaded_documents(user_ids)
            with SessionLocal() as session:
                session.execute(delete(UserOrgTag).where(UserOrgTag.user_id.in_(user_ids)))
                session.execute(delete(AppUser).where(AppUser.id.in_(user_ids)))
                session.execute(
                    delete(OrgTag).where(OrgTag.name.in_(list(tag_names.values())))
                )
                session.commit()


def _headers(account: dict) -> dict:
    return {"Authorization": f"Bearer {account['access_token']}"}


def _upload(client, prefix: str, account: dict, scope: str, visibility: str, name: str):
    resp = client.post(
        f"{prefix}/documents",
        headers=_headers(account),
        files={"file": (name, f"空间测试内容 {name} {uuid4().hex}".encode(), "text/plain")},
        data={"scope": scope, "visibility": visibility},
    )
    assert resp.status_code == 202, resp.text
    return resp.json()


def _visible_titles(client, prefix: str, account: dict, scope: str = "all") -> set[str]:
    data = client.get(
        f"{prefix}/documents?limit=200&scope={scope}", headers=_headers(account)
    ).json()
    return {item["title"] for item in data["items"]}


def test_upload_scope_visibility_matrix(space_env):
    client, prefix, ctx = space_env
    jia, yi = ctx["accounts"]["甲"], ctx["accounts"]["乙"]
    tech_tag = ctx["tag_names"]["甲"]

    docs = {
        "org_public": _upload(client, prefix, jia, tech_tag, "public", "组织公开.txt"),
        "org_private": _upload(client, prefix, jia, tech_tag, "private", "组织私有.txt"),
        "personal_public": _upload(client, prefix, jia, "personal", "public", "个人公开.txt"),
        "personal_private": _upload(client, prefix, jia, "personal", "private", "个人私有.txt"),
    }
    for doc in docs.values():
        assert doc["kb_id"] > 0  # 全部落在主空间

    # 上传者自己：四个都可见
    own = _visible_titles(client, prefix, jia)
    assert {"组织公开", "组织私有", "个人公开", "个人私有"} <= own

    # 乙（市场部）：组织公开 + 个人公开；技术与乙的私有不可见
    other = _visible_titles(client, prefix, yi)
    assert "组织公开" in other and "个人公开" in other
    assert "组织私有" not in other and "个人私有" not in other

    # 响应携带归属信息
    listed = client.get(f"{prefix}/documents?limit=200", headers=_headers(jia)).json()
    by_title = {item["title"]: item for item in listed["items"]}
    assert by_title["组织公开"]["org_tag_name"] == tech_tag
    assert by_title["个人私有"]["org_tag_name"] is None
    assert by_title["个人私有"]["visibility"] == "private"

    # scope 过滤
    personal = _visible_titles(client, prefix, jia, scope="personal")
    assert "组织公开" not in personal and "个人私有" in personal
    org_only = _visible_titles(client, prefix, jia, scope=tech_tag)
    assert "组织公开" in org_only and "个人公开" not in org_only


def test_upload_scope_permissions(space_env):
    client, prefix, ctx = space_env
    yi = ctx["accounts"]["乙"]

    # 向他人组织传 → 403；未知组织 → 404
    forbidden = client.post(
        f"{prefix}/documents",
        headers=_headers(yi),
        files={"file": ("x.txt", b"content", "text/plain")},
        data={"scope": ctx["tag_names"]["甲"], "visibility": "private"},
    )
    assert forbidden.status_code == 403
    missing = client.post(
        f"{prefix}/documents",
        headers=_headers(yi),
        files={"file": ("x.txt", b"content", "text/plain")},
        data={"scope": "不存在的组织", "visibility": "private"},
    )
    assert missing.status_code == 404
    bad_visibility = client.post(
        f"{prefix}/documents",
        headers=_headers(yi),
        files={"file": ("x.txt", b"content", "text/plain")},
        data={"scope": "personal", "visibility": "internal"},
    )
    assert bad_visibility.status_code == 422


def test_delete_permission_uploader_and_admin(space_env):
    client, prefix, ctx = space_env
    jia, yi = ctx["accounts"]["甲"], ctx["accounts"]["乙"]
    admin_headers = {"X-API-Token": get_settings().api_token}

    uploaded = _upload(client, prefix, jia, "personal", "private", "待删除.txt")

    # 乙（非上传者、非成员）→ 403；管理员 → 200；上传者再传再删 → 200
    deleted = client.delete(
        f"{prefix}/documents/{uploaded['document_id']}", headers=_headers(yi)
    )
    assert deleted.status_code == 403
    assert (
        client.delete(
            f"{prefix}/documents/{uploaded['document_id']}", headers=admin_headers
        ).status_code
        == 200
    )
    again = _upload(client, prefix, jia, "personal", "private", "待删除2.txt")
    assert (
        client.delete(
            f"{prefix}/documents/{again['document_id']}", headers=_headers(jia)
        ).status_code
        == 200
    )


def test_job_polling_by_uploader_without_membership(space_env):
    client, prefix, ctx = space_env
    jia = ctx["accounts"]["甲"]
    uploaded = _upload(client, prefix, jia, "personal", "private", "任务轮询.txt")
    resp = client.get(f"{prefix}/jobs/{uploaded['job_id']}", headers=_headers(jia))
    assert resp.status_code == 200
    assert resp.json()["job_type"] == "ingest_file"

    # 他人（无成员身份）不能轮询甲的任务
    yi = ctx["accounts"]["乙"]
    assert (
        client.get(f"{prefix}/jobs/{uploaded['job_id']}", headers=_headers(yi)).status_code
        == 403
    )


def test_qa_without_kb_id_targets_main_space(space_env):
    client, prefix, ctx = space_env
    jia = ctx["accounts"]["甲"]
    _upload(client, prefix, jia, "personal", "public", "主空间问答.txt")
    resp = client.post(
        f"{prefix}/qa",
        headers=_headers(jia),
        json={"question": "主空间问答这三个字出现在哪份文档？"},
    )
    # 共享 dev 库上 worker 可能正在重建主空间：装载期修订号变化返回 409 也算路由成功；
    # 关键契约是 kb_id 缺省不再 422
    assert resp.status_code in (200, 409), resp.text
    if resp.status_code == 200:
        assert resp.json()["status"] in ("answered", "insufficient_evidence")


def test_main_space_is_protected(space_env):
    client, prefix, ctx = space_env
    admin_headers = {"X-API-Token": get_settings().api_token}
    reserved = client.post(f"{prefix}/kbs", headers=admin_headers, json={"name": "主空间"})
    assert reserved.status_code == 409
    with SessionLocal() as session:
        from app.db.models import KbCollection

        main_id = session.scalar(
            select(KbCollection.id).where(KbCollection.name == "主空间")
        )
    assert client.delete(f"{prefix}/kbs/{main_id}", headers=admin_headers).status_code == 409


def test_kb_member_still_can_access_legacy_kb(space_env):
    """旧机制回归：成员制知识库的 viewer 仍不能删别人的文档。"""
    client, prefix, ctx = space_env
    jia, yi = ctx["accounts"]["甲"], ctx["accounts"]["乙"]
    owner_headers = _headers(jia)
    kb_id = client.post(
        f"{prefix}/kbs", headers=owner_headers, json={"name": f"旧机制回归-{uuid4().hex[:8]}"}
    ).json()["kb_id"]
    try:
        with SessionLocal() as session:
            session.add(KbMember(kb_id=kb_id, user_id=yi["user"]["user_id"], role="viewer"))
            session.commit()
        uploaded = client.post(
            f"{prefix}/kbs/{kb_id}/documents",
            headers=owner_headers,
            files={"file": ("旧机制.txt", b"legacy", "text/plain")},
        )
        assert uploaded.status_code == 202
        deleted = client.delete(
            f"{prefix}/documents/{uploaded.json()['document_id']}", headers=_headers(yi)
        )
        assert deleted.status_code == 403
    finally:
        cleanup_kb(kb_id)


def test_patch_document_scope_and_visibility(space_env, tmp_path):
    """归属/可见性修改：文档与块级冗余列同步，acl_revision 递增，可见范围即时变化。"""
    from app.db.session import SessionLocal
    from app.gateways.mock import MockEmbeddingGateway
    from app.indexing.snapshot import IndexBuilder

    client, prefix, ctx = space_env
    jia, yi = ctx["accounts"]["甲"], ctx["accounts"]["乙"]
    tech_tag = ctx["tag_names"]["甲"]
    admin_headers = {"X-API-Token": get_settings().api_token}

    uploaded = _upload(client, prefix, jia, "personal", "private", "改归属.txt")
    document_id = uploaded["document_id"]
    with SessionLocal() as session:
        doc_version_id = session.scalar(
            select(KbDocumentVersion.id).where(
                KbDocumentVersion.document_id == document_id
            )
        )
        acl_before = session.get(KbCollection, uploaded["kb_id"]).acl_revision
    # 真实构建快照，使块级冗余列存在
    builder = IndexBuilder(
        SessionLocal, MockEmbeddingGateway(dim=1024), get_settings(), worker_id="space-it"
    )
    asyncio.run(builder.build(kb_id=uploaded["kb_id"], new_doc_version_id=doc_version_id))

    # 个人私有 → 组织公开：乙（另一组织）立即不可见 → 变更后立即可见
    assert "改归属" not in _visible_titles(client, prefix, yi)
    patched = client.patch(
        f"{prefix}/documents/{document_id}",
        headers=_headers(jia),
        json={"scope": tech_tag, "visibility": "public"},
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["org_tag_name"] == tech_tag and body["visibility"] == "public"
    assert body["uploader_user_id"] == jia["user"]["user_id"]
    assert "改归属" in _visible_titles(client, prefix, yi)

    with SessionLocal() as session:
        chunk_rows = session.execute(
            select(KbChunk.org_tag_id, KbChunk.visibility).where(
                KbChunk.document_id == document_id
            )
        ).all()
        assert chunk_rows and {row[0] for row in chunk_rows} != {None}
        assert {row[1] for row in chunk_rows} == {"public"}
        assert session.get(KbCollection, uploaded["kb_id"]).acl_revision == acl_before + 1

    # 校验与权限：未知组织 404 / 非法可见性 422 / 非上传者非管理员 403 / 空体 422
    assert (
        client.patch(
            f"{prefix}/documents/{document_id}",
            headers=_headers(jia),
            json={"scope": "不存在的组织"},
        ).status_code
        == 404
    )
    assert (
        client.patch(
            f"{prefix}/documents/{document_id}",
            headers=_headers(jia),
            json={"visibility": "internal"},
        ).status_code
        == 422
    )
    assert (
        client.patch(
            f"{prefix}/documents/{document_id}",
            headers=_headers(yi),
            json={"visibility": "public"},
        ).status_code
        == 403
    )
    assert (
        client.patch(
            f"{prefix}/documents/{document_id}", headers=_headers(jia), json={}
        ).status_code
        == 422
    )

    # 改回个人私有：乙立即不可见
    reverted = client.patch(
        f"{prefix}/documents/{document_id}",
        headers=admin_headers,
        json={"scope": "personal", "visibility": "private"},
    )
    assert reverted.status_code == 200
    assert reverted.json()["org_tag_name"] is None
    assert "改归属" not in _visible_titles(client, prefix, yi)
    # 上传者本人始终可见
    assert "改归属" in _visible_titles(client, prefix, jia)
