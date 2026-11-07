"""任务/会话接口回归：列表/创建/详情还原/删除与问答挂接。

对话数据来自 qa_request 审计；详情还原校验消息序列与引用（document_version join）。
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.core.config import get_settings
from app.db.models import (
    AppUser,
    Conversation,
    IngestJob,
    KbDocument,
    KbDocumentVersion,
    QaCitation,
    QaRequest,
)
from app.db.session import SessionLocal
from app.main import app
from tests.integration.helpers import cleanup_kb


@pytest.fixture
def conv_env(pg_ready):
    """两个用户（A/B）；A 有一个空知识库（未建索引 → 问答必走拒答路径，不调模型）。"""
    prefix = get_settings().api_prefix
    accounts = {}
    kb_ids = []
    with TestClient(app) as client:
        for key in ("A", "B"):
            payload = {
                "username": f"conv_{uuid4().hex}",
                "password": "test-password",
                "display_name": f"会话-{key}",
            }
            resp = client.post(f"{prefix}/auth/register", json=payload)
            assert resp.status_code == 201, resp.text
            accounts[key] = resp.json()
        # 空知识库经 API 创建：owner 成员自动写入（裸 ORM 建库无成员会被拒）
        kb_id = client.post(
            f"{prefix}/kbs",
            headers=_headers(accounts["A"]),
            json={"name": f"会话空库-{uuid4().hex[:8]}"},
        ).json()["kb_id"]
        kb_ids.append(kb_id)
        admin_headers = {"X-API-Token": get_settings().api_token}
        try:
            yield client, prefix, {"accounts": accounts, "kb_id": kb_id, "admin": admin_headers}
        finally:
            for kb in kb_ids:
                cleanup_kb(kb)
            user_ids = [a["user"]["user_id"] for a in accounts.values()]
            with SessionLocal() as session:
                conv_ids = session.scalars(
                    select(Conversation.id).where(Conversation.user_id.in_(user_ids))
                ).all()
                if conv_ids:
                    req_ids = session.scalars(
                        select(QaRequest.id).where(QaRequest.conversation_id.in_(conv_ids))
                    ).all()
                    if req_ids:
                        session.execute(delete(QaCitation).where(QaCitation.qa_request_id.in_(req_ids)))
                    session.execute(delete(QaRequest).where(QaRequest.id.in_(req_ids)))
                    session.execute(delete(Conversation).where(Conversation.id.in_(conv_ids)))
                session.execute(delete(AppUser).where(AppUser.id.in_(user_ids)))
                session.commit()


def _headers(account: dict) -> dict:
    return {"Authorization": f"Bearer {account['access_token']}"}


def _create(client, prefix, account, title: str) -> int:
    resp = client.post(f"{prefix}/conversations", headers=_headers(account), json={"title": title})
    assert resp.status_code == 201, resp.text
    return resp.json()["conversation_id"]


def test_conversation_crud_and_qa_linkage(conv_env):
    client, prefix, ctx = conv_env
    a = ctx["accounts"]["A"]

    conv_id = _create(client, prefix, a, "报销期限咨询")

    # 列表：total/排序字段
    listed = client.get(f"{prefix}/conversations", headers=_headers(a)).json()
    assert listed["total"] == 1
    item = listed["items"][0]
    assert item["conversation_id"] == conv_id and item["title"] == "报销期限咨询"
    assert item["updated_at"] is not None

    # 问答挂接：空库 → insufficient_evidence（不调模型），审计带 conversation_id
    stream = client.post(
        f"{prefix}/qa/stream",
        headers=_headers(a),
        json={"kb_id": ctx["kb_id"], "conversation_id": conv_id, "question": "报销期限是多久"},
    )
    assert stream.status_code == 200
    assert '"status": "insufficient_evidence"' in stream.text

    with SessionLocal() as session:
        qa = session.query(QaRequest).filter_by(conversation_id=conv_id).one()
        assert qa.answer_status == "insufficient_evidence"
        conv = session.get(Conversation, conv_id)
        assert conv.updated_at >= qa.created_at  # 最近使用时间已刷新

    # 详情还原：user + assistant 两条消息
    detail = client.get(f"{prefix}/conversations/{conv_id}", headers=_headers(a)).json()
    assert detail["title"] == "报销期限咨询"
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
    assert detail["messages"][0]["content"] == "报销期限是多久"
    assert detail["messages"][1]["status"] == "insufficient_evidence"
    assert detail["messages"][1]["request_id"] == qa.request_id

    # 删除 → 列表空 + 详情 404
    deleted = client.delete(f"{prefix}/conversations/{conv_id}", headers=_headers(a))
    assert deleted.status_code == 204
    assert client.get(f"{prefix}/conversations/{conv_id}", headers=_headers(a)).status_code == 404


def test_conversation_ownership_and_auth(conv_env):
    client, prefix, ctx = conv_env
    a, b = ctx["accounts"]["A"], ctx["accounts"]["B"]
    conv_id = _create(client, prefix, a, "甲的会话")

    # 他人读写删一律 404（不泄露存在性）
    assert client.get(f"{prefix}/conversations/{conv_id}", headers=_headers(b)).status_code == 404
    deleted = client.delete(f"{prefix}/conversations/{conv_id}", headers=_headers(b))
    assert deleted.status_code == 404
    # 借他人会话提问（缺省主空间，绕过 kb 成员校验）→ 会话归属检查 404
    assert (
        client.post(
            f"{prefix}/qa/stream",
            headers=_headers(b),
            json={"conversation_id": conv_id, "question": "x"},
        ).status_code
        == 404
    )

    # 未登录 401
    assert client.get(f"{prefix}/conversations").status_code == 401

    # 非法参数：空标题 422 / limit 越界 422
    created = client.post(f"{prefix}/conversations", headers=_headers(a), json={"title": "  "})
    assert created.status_code == 422
    assert (
        client.get(f"{prefix}/conversations?limit=500", headers=_headers(a)).status_code == 422
    )


def test_list_ordering_by_recent_activity(conv_env):
    client, prefix, ctx = conv_env
    a = ctx["accounts"]["A"]
    first = _create(client, prefix, a, "最早创建")
    second = _create(client, prefix, a, "最近更新")

    # 手动把第一条的 updated_at 调到过去 → 列表第二条在前；再问答刷新后顺序翻转
    past = datetime.now(UTC) - timedelta(hours=2)
    future = datetime.now(UTC) - timedelta(hours=1)
    with SessionLocal() as session:
        session.get(Conversation, first).updated_at = past
        session.get(Conversation, second).updated_at = future
        session.commit()
    items = client.get(f"{prefix}/conversations", headers=_headers(a)).json()["items"]
    assert [i["conversation_id"] for i in items] == [second, first]

    stream = client.post(
        f"{prefix}/qa/stream",
        headers=_headers(a),
        json={"kb_id": ctx["kb_id"], "conversation_id": first, "question": "刷新时间"},
    )
    assert stream.status_code == 200
    items = client.get(f"{prefix}/conversations", headers=_headers(a)).json()["items"]
    assert items[0]["conversation_id"] == first


def test_detail_restores_citations_with_document_version(conv_env):
    """引用还原：直接构造审计行（避免真实模型调用），校验 document_version join 与顺序。"""
    client, prefix, ctx = conv_env
    a = ctx["accounts"]["A"]
    conv_id = _create(client, prefix, a, "带引用的会话")

    # 在空知识库里塞一份文档 + 版本（不构建索引，仅用于引用 join）
    kb_id = ctx["kb_id"]
    with SessionLocal() as session:
        document = KbDocument(
            kb_id=kb_id, title="引用还原测试", uploader_user_id=1, visibility="public"
        )
        session.add(document)
        session.flush()
        version = KbDocumentVersion(
            kb_id=kb_id,
            document_id=document.id,
            version_no=3,
            title=document.title,
            file_sha256=b"0" * 32,
            storage_uri="uploads/fake.txt",
            parse_status="succeeded",
        )
        session.add(version)
        session.flush()
        qa = QaRequest(
            request_id=f"qa_{uuid4().hex}",
            tenant_id=1,
            user_id=a["user"]["user_id"],
            kb_id=kb_id,
            conversation_id=conv_id,
            question="带引用的问题",
            answer_status="answered",
            retrieval_mode="hybrid",
            answer="依据[S1]的结论。",
            model_config_json={},
        )
        session.add(qa)
        session.flush()
        session.add(
            QaCitation(
                qa_request_id=qa.id,
                source_id="S1",
                citation_id=f"c_{uuid4().hex[:12]}",
                chunk_id=99999999,
                doc_version_id=version.id,
                document_title=document.title,
                locator={"type": "line", "spans": [{"line": 7}]},
                quote="结论原文",
            )
        )
        session.commit()

    detail = client.get(f"{prefix}/conversations/{conv_id}", headers=_headers(a)).json()
    assistant = detail["messages"][-1]
    assert assistant["status"] == "answered"
    assert assistant["answer"] == "依据[S1]的结论。"
    assert len(assistant["citations"]) == 1
    citation = assistant["citations"][0]
    assert citation["document_version"] == 3
    assert citation["document_title"] == "引用还原测试"
    assert citation["locator"] == {"type": "line", "spans": [{"line": 7}]}

    # 清理这份手工文档（版本随 cleanup_kb 处理；块无）
    with SessionLocal() as session:
        session.execute(delete(QaCitation).where(QaCitation.qa_request_id == qa.id))
        session.execute(delete(QaRequest).where(QaRequest.id == qa.id))
        session.execute(delete(KbDocumentVersion).where(KbDocumentVersion.id == version.id))
        session.execute(delete(KbDocument).where(KbDocument.id == document.id))
        session.execute(delete(IngestJob).where(IngestJob.kb_id == kb_id))
        session.commit()
