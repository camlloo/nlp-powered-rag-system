"""JWT 用户从登录到知识库、流式问答与角色权限的回归。"""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.core.auth import get_admin_principal
from app.core.config import get_settings
from app.db.models import AppUser, KbDocument, KbDocumentVersion, KbMember, OrgTag, UserOrgTag
from app.db.session import SessionLocal
from app.gateways.mock import MockChatGateway, MockEmbeddingGateway
from app.generation.service import QaService
from app.indexing.cache import IndexCacheManager
from app.main import app
from app.retrieval.service import RetrievalService
from tests.integration.helpers import cleanup_kb


@pytest.fixture
def users(pg_ready):
    prefix = get_settings().api_prefix
    accounts = []
    tag_name = f"regression-{uuid4().hex}"
    with TestClient(app) as client:
        try:
            for _ in range(2):
                payload = {
                    "username": f"user_{uuid4().hex}", "password": "test-password",
                    "display_name": "联调测试", "org_tags": [tag_name, f" {tag_name} "],
                }
                response = client.post(f"{prefix}/auth/register", json=payload)
                assert response.status_code == 201, response.text
                data = response.json()
                assert data["user"]["org_tags"] == [tag_name]
                accounts.append({**data, "credentials": payload})
            yield client, prefix, accounts
        finally:
            user_ids = [account["user"]["user_id"] for account in accounts]
            with SessionLocal() as session:
                kb_ids = session.scalars(
                    select(KbMember.kb_id).where(KbMember.user_id.in_(user_ids))
                ).all()
            for kb_id in set(kb_ids):
                cleanup_kb(kb_id)
            with SessionLocal() as session:
                session.execute(delete(UserOrgTag).where(UserOrgTag.user_id.in_(user_ids)))
                session.execute(delete(AppUser).where(AppUser.id.in_(user_ids)))
                session.execute(delete(OrgTag).where(OrgTag.name == tag_name))
                session.commit()


def test_login_me_kb_list_and_stream_with_jwt(users):
    client, prefix, accounts = users
    owner, other = accounts
    login = client.post(f"{prefix}/auth/login", json={
        "username": owner["credentials"]["username"], "password": "test-password",
    })
    assert login.status_code == 200
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    assert client.get(f"{prefix}/auth/me", headers=headers).json() == owner["user"]
    kb = client.post(f"{prefix}/kbs", headers=headers, json={"name": "JWT 知识库"}).json()
    listed = client.get(f"{prefix}/kbs", headers=headers).json()["items"]
    assert kb["kb_id"] in [item["kb_id"] for item in listed]
    other_headers = {"Authorization": f"Bearer {other['access_token']}"}
    other_list = client.get(f"{prefix}/kbs", headers=other_headers).json()["items"]
    assert kb["kb_id"] not in [item["kb_id"] for item in other_list]
    history = [
        {"role": role, "content": "报销规定"}
        for _ in range(3) for role in ("user", "assistant")
    ]
    stream = client.post(f"{prefix}/qa/stream", headers=headers, json={
        "kb_id": kb["kb_id"], "question": "报销期限", "history": history,
    })
    assert stream.status_code == 200
    assert "event: done" in stream.text
    assert '"status": "insufficient_evidence"' in stream.text


@pytest.mark.parametrize("role", ["viewer", "editor"])
def test_member_write_operations_enforce_role(users, role):
    client, prefix, accounts = users
    owner, member = accounts
    owner_headers = {"Authorization": f"Bearer {owner['access_token']}"}
    headers = {"Authorization": f"Bearer {member['access_token']}"}
    kb_id = client.post(
        f"{prefix}/kbs", headers=owner_headers, json={"name": "角色测试"}
    ).json()["kb_id"]
    with SessionLocal() as session:
        session.add(KbMember(kb_id=kb_id, user_id=member["user"]["user_id"], role=role))
        session.commit()
    upload = client.post(f"{prefix}/kbs/{kb_id}/documents", headers=owner_headers, files={
        "file": ("policy.txt", "报销应在三十日内完成。".encode(), "text/plain"),
    })
    assert upload.status_code == 202
    document_id = upload.json()["document_id"]
    expected = 403 if role == "viewer" else 202
    assert client.post(f"{prefix}/kbs/{kb_id}/reindex", headers=headers).status_code == expected
    expected = 403 if role == "viewer" else 200
    deleted = client.delete(f"{prefix}/documents/{document_id}", headers=headers)
    assert deleted.status_code == expected
    assert client.delete(f"{prefix}/kbs/{kb_id}", headers=headers).status_code == 403


def test_private_citation_is_hidden_from_other_kb_members(users, indexed_kb):
    import asyncio

    client, prefix, accounts = users
    kb_id, settings = indexed_kb
    other = accounts[1]
    service = QaService(
        RetrievalService(IndexCacheManager(settings), MockEmbeddingGateway(dim=1024), settings),
        MockChatGateway(), settings,
    )
    outcome = asyncio.run(service.ask(
        kb_id=kb_id, principal=get_admin_principal(), question="报销期限",
    ))
    citation = outcome.citations[0]
    with SessionLocal() as session:
        document = session.scalar(
            select(KbDocument)
            .join(KbDocumentVersion, KbDocumentVersion.document_id == KbDocument.id)
            .where(KbDocumentVersion.id == citation.doc_version_id)
        )
        document.visibility = "private"
        document.uploader_user_id = accounts[0]["user"]["user_id"]
        document.org_tag_id = None
        document_id = document.id
        session.add(KbMember(kb_id=kb_id, user_id=other["user"]["user_id"], role="viewer"))
        session.commit()
    headers = {"Authorization": f"Bearer {other['access_token']}"}
    path = f"{prefix}/citations/{citation.citation_id}"
    assert client.get(path, headers=headers).status_code == 404
    with SessionLocal() as session:
        session.get(KbDocument, document_id).visibility = "public"
        session.commit()
    assert client.get(path, headers=headers).status_code == 200
