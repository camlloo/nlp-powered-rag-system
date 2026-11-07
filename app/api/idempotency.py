"""进程内 Idempotency-Key 存储（实现文档 §9.2）。

语义：相同用户、相同接口、相同键及相同请求摘要返回既有结果；
同键不同内容返回 409。

多进程/多实例部署需以独立迁移补表持久化（实现文档附录 A 前言）；
首期单 API 进程，进程内存储即可。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

_TTL_SECONDS = 24 * 3600


@dataclass
class IdempotencyRecord:
    digest: str
    status_code: int
    body: dict
    created_at: float


class IdempotencyStore:
    def __init__(self, ttl_seconds: int = _TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._records: dict[tuple[int, str, str], IdempotencyRecord] = {}

    def _prune(self, now: float) -> None:
        expired = [k for k, r in self._records.items() if now - r.created_at > self._ttl]
        for k in expired:
            del self._records[k]

    def check(
        self, *, key: str, user_id: int, route: str, digest: str
    ) -> tuple[str, IdempotencyRecord | None]:
        """返回 ("new" | "replay" | "conflict", 记录)。"""
        self._prune(time.time())
        record = self._records.get((user_id, route, key))
        if record is None:
            return "new", None
        if record.digest != digest:
            return "conflict", record
        return "replay", record

    def save(
        self,
        *,
        key: str,
        user_id: int,
        route: str,
        digest: str,
        status_code: int,
        body: dict,
    ) -> None:
        self._prune(time.time())
        self._records[(user_id, route, key)] = IdempotencyRecord(
            digest=digest, status_code=status_code, body=body, created_at=time.time()
        )


idempotency_store = IdempotencyStore()
