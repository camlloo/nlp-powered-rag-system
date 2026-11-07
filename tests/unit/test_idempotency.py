"""Idempotency-Key 存储单测（实现文档 §9.2 语义）。"""

import time

from app.api.idempotency import IdempotencyStore


def test_new_replay_conflict():
    store = IdempotencyStore()
    outcome, record = store.check(key="k1", user_id=1, route="r", digest="d1")
    assert outcome == "new" and record is None

    store.save(key="k1", user_id=1, route="r", digest="d1", status_code=202, body={"job_id": 9})

    outcome, record = store.check(key="k1", user_id=1, route="r", digest="d1")
    assert outcome == "replay"
    assert record.body == {"job_id": 9}
    assert record.status_code == 202

    outcome, _ = store.check(key="k1", user_id=1, route="r", digest="d2")
    assert outcome == "conflict"  # 同键不同内容


def test_scoped_by_user_and_route():
    store = IdempotencyStore()
    store.save(key="k", user_id=1, route="r1", digest="d", status_code=202, body={})
    assert store.check(key="k", user_id=2, route="r1", digest="d")[0] == "new"
    assert store.check(key="k", user_id=1, route="r2", digest="d")[0] == "new"


def test_ttl_prune():
    store = IdempotencyStore(ttl_seconds=10)
    store.save(key="k", user_id=1, route="r", digest="d", status_code=202, body={})
    record = store._records[(1, "r", "k")]
    record.created_at = time.time() - 100  # 人为过期
    assert store.check(key="k", user_id=1, route="r", digest="d")[0] == "new"
    assert (1, "r", "k") not in store._records
