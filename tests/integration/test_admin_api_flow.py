"""管理面接口回归：组织标签 CRUD、用户管理与可见性联动（管理员专用）。

管理员以静态令牌旁路（X-API-Token）进入；普通用户走 JWT，验证 403。
标签删除/打标对文档可见性的影响即时生效（principal 每请求重查）。
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
    KbDocument,
    KbDocumentVersion,
    KbMember,
    OrgTag,
    UserOrgTag,
)
from app.db.session import SessionLocal
from app.gateways.mock import MockEmbeddingGateway
from app.indexing.snapshot import IndexBuilder
from app.main import app
from tests.integration.helpers import cleanup_kb, create_kb


@pytest.fixture
def admin_env(pg_ready, tmp_path):
    """3 名用户（上传者/查看者带同一标签，普通用户无标签）+ 一个成员制知识库。"""
    prefix = get_settings().api_prefix
    tag_name = f"admin-it-{uuid4().hex}"
    accounts: dict[str, dict] = {}
    kb_id = create_kb()
    with TestClient(app) as client:
        for key, tags in (("uploader", [tag_name]), ("viewer", [tag_name]), ("plain", [])):
            payload = {
                "username": f"{key}_{uuid4().hex}",
                "password": "test-password",
                "display_name": f"管理面-{key}",
                "org_tags": tags,
            }
            resp = client.post(f"{prefix}/auth/register", json=payload)
            assert resp.status_code == 201, resp.text
            accounts[key] = {**resp.json(), "credentials": payload}
        with SessionLocal() as session:
            for key in ("uploader", "viewer"):
                session.add(
                    KbMember(kb_id=kb_id, user_id=accounts[key]["user"]["user_id"], role="editor")
                )
            session.commit()
        ctx = {
            "tag_name": tag_name,
            "admin_headers": {"X-API-Token": get_settings().api_token},
            "accounts": accounts,
            "kb_id": kb_id,
            "tmp_path": tmp_path,
            "created_tag_ids": [],  # 用例中经 API 创建的标签，收尾统一清理
        }
        try:
            yield client, prefix, ctx
        finally:
            cleanup_kb(kb_id)
            user_ids = [account["user"]["user_id"] for account in accounts.values()]
            created_ids = ctx["created_tag_ids"]
            with SessionLocal() as session:
                session.execute(delete(UserOrgTag).where(UserOrgTag.user_id.in_(user_ids)))
                session.execute(delete(AppUser).where(AppUser.id.in_(user_ids)))
                session.execute(delete(OrgTag).where(OrgTag.name == tag_name))
                if created_ids:
                    session.execute(delete(UserOrgTag).where(UserOrgTag.org_tag_id.in_(created_ids)))
                    session.execute(delete(OrgTag).where(OrgTag.id.in_(created_ids)))
                session.commit()


def _jwt_headers(account: dict) -> dict:
    return {"Authorization": f"Bearer {account['access_token']}"}


def _find_tag(client, prefix: str, name: str, headers: dict) -> dict:
    items = client.get(f"{prefix}/admin/org-tags", headers=headers).json()["items"]
    return next(item for item in items if item["name"] == name)


def _upload_as(client, prefix: str, kb_id: int, account: dict) -> dict:
    resp = client.post(
        f"{prefix}/kbs/{kb_id}/documents",
        headers=_jwt_headers(account),
        files={"file": ("组织隔离.txt", "第五章 组织隔离样例内容。".encode(), "text/plain")},
    )
    assert resp.status_code == 202, resp.text
    return resp.json()


def test_org_tag_crud_counts_and_admin_gate(admin_env):
    client, prefix, ctx = admin_env
    admin_headers = ctx["admin_headers"]
    plain_headers = _jwt_headers(ctx["accounts"]["plain"])

    # 普通用户与管理员门槛：403 / 401
    assert client.get(f"{prefix}/admin/org-tags", headers=plain_headers).status_code == 403
    assert client.get(f"{prefix}/admin/org-tags").status_code == 401

    listed = client.get(f"{prefix}/admin/org-tags", headers=admin_headers).json()
    mine = next(item for item in listed["items"] if item["name"] == ctx["tag_name"])
    assert listed["total"] >= 1
    assert mine["user_count"] == 2  # uploader + viewer
    assert mine["document_count"] == 0

    # 创建：成功 / 重名 409 / 空名 422
    fresh_name = f"fresh-{uuid4().hex}"
    created = client.post(
        f"{prefix}/admin/org-tags", headers=admin_headers, json={"name": fresh_name}
    )
    assert created.status_code == 201, created.text
    ctx["created_tag_ids"].append(created.json()["tag_id"])
    assert created.json()["user_count"] == 0
    assert (
        client.post(
            f"{prefix}/admin/org-tags", headers=admin_headers, json={"name": ctx["tag_name"]}
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"{prefix}/admin/org-tags", headers=admin_headers, json={"name": "  "}
        ).status_code
        == 422
    )

    # 重命名：成功 / 重名 409 / 不存在 404
    renamed_name = f"renamed-{uuid4().hex}"
    renamed = client.patch(
        f"{prefix}/admin/org-tags/{created.json()['tag_id']}",
        headers=admin_headers,
        json={"name": renamed_name},
    )
    assert renamed.status_code == 200
    assert renamed.json()["name"] == renamed_name
    assert (
        client.patch(
            f"{prefix}/admin/org-tags/{created.json()['tag_id']}",
            headers=admin_headers,
            json={"name": ctx["tag_name"]},
        ).status_code
        == 409
    )
    assert (
        client.patch(
            f"{prefix}/admin/org-tags/99999999", headers=admin_headers, json={"name": "x"}
        ).status_code
        == 404
    )


def test_delete_tag_detaches_references_and_bumps_acl_revision(admin_env):
    client, prefix, ctx = admin_env
    admin_headers = ctx["admin_headers"]
    kb_id = ctx["kb_id"]

    uploaded = _upload_as(client, prefix, kb_id, ctx["accounts"]["uploader"])
    with SessionLocal() as session:
        document = session.get(KbDocument, uploaded["document_id"])
        assert document.org_tag_id is not None
        tag_id = document.org_tag_id
        kb_before = session.get(KbCollection, kb_id)
        acl_before = kb_before.acl_revision

    # 真实构建快照，块级冗余列随文档继承
    with SessionLocal() as session:
        doc_version_id = session.scalar(
            select(KbDocumentVersion.id).where(
                KbDocumentVersion.kb_id == kb_id,
                KbDocumentVersion.document_id == uploaded["document_id"],
            )
        )
    builder = IndexBuilder(
        SessionLocal, MockEmbeddingGateway(dim=1024), get_settings(), worker_id="admin-it"
    )
    asyncio.run(builder.build(kb_id=kb_id, new_doc_version_id=doc_version_id))
    with SessionLocal() as session:
        chunk_tag_ids = session.scalars(
            select(KbChunk.org_tag_id).where(KbChunk.document_id == uploaded["document_id"])
        ).all()
    assert chunk_tag_ids and set(chunk_tag_ids) == {tag_id}

    deleted = client.delete(f"{prefix}/admin/org-tags/{tag_id}", headers=admin_headers)
    assert deleted.status_code == 200, deleted.text
    body = deleted.json()
    assert body["detached_documents"] == 1
    assert body["kbs_touched"] == [kb_id]

    with SessionLocal() as session:
        assert session.get(OrgTag, tag_id) is None
        document = session.get(KbDocument, uploaded["document_id"])
        assert document.org_tag_id is None
        chunk_tag_ids = session.scalars(
            select(KbChunk.org_tag_id).where(KbChunk.document_id == uploaded["document_id"])
        ).all()
        assert set(chunk_tag_ids) == {None}
        assert session.get(KbCollection, kb_id).acl_revision == acl_before + 1
        assert not session.scalars(
            select(UserOrgTag).where(UserOrgTag.org_tag_id == tag_id)
        ).all()
    repeat = client.delete(f"{prefix}/admin/org-tags/{tag_id}", headers=admin_headers)
    assert repeat.status_code == 404


def test_user_admin_promotion_last_admin_guard_and_tag_assignment(admin_env):
    client, prefix, ctx = admin_env
    admin_headers = ctx["admin_headers"]
    accounts = ctx["accounts"]

    # 用户清单：带标签的用户能看到自己的标签
    listed = client.get(f"{prefix}/admin/users", headers=admin_headers).json()
    assert listed["total"] >= 3
    viewer_item = next(
        u for u in listed["items"] if u["user_id"] == accounts["viewer"]["user"]["user_id"]
    )
    assert viewer_item["org_tags"] == [ctx["tag_name"]]
    assert viewer_item["is_admin"] is False

    # 提升为管理员 → 再撤销时因是最后一名管理员而 409
    plain_id = accounts["plain"]["user"]["user_id"]
    promoted = client.patch(
        f"{prefix}/admin/users/{plain_id}", headers=admin_headers, json={"is_admin": True}
    )
    assert promoted.status_code == 200
    assert promoted.json()["is_admin"] is True

    # 守卫需确定触发：临时撤销库内其他管理员（finally 恢复原状，不污染共享开发库）
    with SessionLocal() as session:
        other_admins = session.scalars(
            select(AppUser).where(AppUser.is_admin.is_(True), AppUser.id != plain_id)
        ).all()
        saved_states = [(u.id, u.is_admin) for u in other_admins]
        for u in other_admins:
            u.is_admin = False
        session.commit()
    try:
        demoted = client.patch(
            f"{prefix}/admin/users/{plain_id}", headers=admin_headers, json={"is_admin": False}
        )
        assert demoted.status_code == 409
        assert demoted.json()["code"] == "conflict"
    finally:
        with SessionLocal() as session:
            for user_id, was_admin in saved_states:
                session.get(AppUser, user_id).is_admin = was_admin
            session.commit()

    # 打标：替换集合 / 未知标签 404 / 未知用户 404
    _find_tag(client, prefix, ctx["tag_name"], admin_headers)  # 前置：目标标签存在
    second_name = f"second-{uuid4().hex}"
    second_id = client.post(
        f"{prefix}/admin/org-tags", headers=admin_headers, json={"name": second_name}
    ).json()["tag_id"]
    ctx["created_tag_ids"].append(second_id)
    tagged = client.put(
        f"{prefix}/admin/users/{plain_id}/org-tags",
        headers=admin_headers,
        json={"tag_ids": [second_id]},
    )
    assert tagged.status_code == 200
    assert tagged.json()["org_tags"] == [second_name]
    assert (
        client.put(
            f"{prefix}/admin/users/{plain_id}/org-tags",
            headers=admin_headers,
            json={"tag_ids": [99999999]},
        ).status_code
        == 404
    )
    assert (
        client.patch(
            f"{prefix}/admin/users/99999999", headers=admin_headers, json={"is_admin": True}
        ).status_code
        == 404
    )


def test_removing_user_tag_takes_document_visibility_away_immediately(admin_env):
    client, prefix, ctx = admin_env
    admin_headers = ctx["admin_headers"]
    kb_id = ctx["kb_id"]
    viewer_headers = _jwt_headers(ctx["accounts"]["viewer"])
    uploader = ctx["accounts"]["uploader"]

    # 上传者带标签上传私有文档 → 同标签的查看者按组织标签可见
    uploaded = _upload_as(client, prefix, kb_id, uploader)
    visible = client.get(f"{prefix}/kbs/{kb_id}/documents", headers=viewer_headers).json()
    assert uploaded["document_id"] in [item["document_id"] for item in visible["items"]]

    # 管理端摘掉查看者的标签 → 同一清单接口立即不可见（principal 每请求重查）
    _find_tag(client, prefix, ctx["tag_name"], admin_headers)  # 前置：目标标签存在
    stripped = client.put(
        f"{prefix}/admin/users/{ctx['accounts']['viewer']['user']['user_id']}/org-tags",
        headers=admin_headers,
        json={"tag_ids": []},
    )
    assert stripped.status_code == 200
    hidden = client.get(f"{prefix}/kbs/{kb_id}/documents", headers=viewer_headers).json()
    assert uploaded["document_id"] not in [item["document_id"] for item in hidden["items"]]

    # 上传者自己的文档依旧可见（所有权匹配），管理员旁路可见全部
    own = client.get(f"{prefix}/kbs/{kb_id}/documents", headers=_jwt_headers(uploader)).json()
    assert uploaded["document_id"] in [item["document_id"] for item in own["items"]]
    admin_view = client.get(f"{prefix}/kbs/{kb_id}/documents", headers=admin_headers).json()
    assert uploaded["document_id"] in [item["document_id"] for item in admin_view["items"]]
