"""后台 worker：任务租约领取、执行与恢复（实现文档 §9.4）。

- 领取：短事务 SELECT FOR UPDATE SKIP LOCKED，写 lease_owner/lease_until/attempt；
- 恢复：租约过期重新可领；超过 max_attempt 的过期任务标记失败；
- 执行：远程模型调用不持有数据库锁；阶段间心跳续租；
- 提交：finish/heartbeat 均以 lease_owner 匹配为条件，旧 worker 不能覆盖新 owner。
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import socket

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.core.errors import AppError
from app.db.models import IngestJob
from app.db.session import SessionLocal
from app.gateways.factory import get_embedding_gateway
from app.indexing.snapshot import IndexBuilder, LeaseKeeper, LeaseLost, RevisionChanged

logger = logging.getLogger(__name__)

WORKER_ID = f"{socket.gethostname()}-{os.getpid()}-{secrets.token_hex(3)}"


def claim_next_job(session: Session, worker_id: str, lease_seconds: int) -> IngestJob | None:
    job = session.execute(
        select(IngestJob)
        .where(
            or_(
                IngestJob.status == "queued",
                and_(IngestJob.status == "running", IngestJob.lease_until < func.now()),
            ),
            IngestJob.attempt < IngestJob.max_attempt,
        )
        .order_by(IngestJob.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    ).scalar_one_or_none()
    if job is None:
        return None
    job.status = "running"
    job.attempt += 1
    job.lease_owner = worker_id
    job.lease_until = func.now() + func.make_interval(0, 0, 0, 0, 0, 0, lease_seconds)
    job.heartbeat_at = func.now()
    session.commit()
    return job


def recover_stuck_jobs(session: Session) -> int:
    """租约过期且重试次数耗尽的任务标记失败。"""

    result = session.execute(
        update(IngestJob)
        .where(
            IngestJob.status == "running",
            IngestJob.lease_until < func.now(),
            IngestJob.attempt >= IngestJob.max_attempt,
        )
        .values(status="failed", error_code="lease_expired")
    )
    session.commit()
    return result.rowcount


def finish_job(
    session_factory: sessionmaker,
    job_id: int,
    worker_id: str,
    *,
    status: str,
    error_code: str | None = None,
    error_detail: str | None = None,
) -> bool:
    """以租约所有者身份结束任务；返回 False 表示租约已易主。"""

    with session_factory() as session:
        result = session.execute(
            update(IngestJob)
            .where(
                IngestJob.id == job_id,
                IngestJob.lease_owner == worker_id,
                IngestJob.status == "running",
            )
            .values(status=status, error_code=error_code, error_detail=error_detail)
        )
        session.commit()
        return result.rowcount > 0


async def execute_claimed(
    job,
    *,
    builder: IndexBuilder,
    session_factory: sessionmaker,
    worker_id: str,
    lease_seconds: int,
) -> None:
    lease = LeaseKeeper(session_factory, worker_id, lease_seconds)
    lease.bind(job.id)
    try:
        if job.job_type in ("ingest_file", "reindex"):
            new_doc_version_id = job.doc_version_id if job.job_type == "ingest_file" else None
            await builder.build(
                kb_id=job.kb_id, new_doc_version_id=new_doc_version_id, lease=lease
            )
        else:
            finish_job(
                session_factory,
                job.id,
                worker_id,
                status="failed",
                error_code="unknown_job_type",
                error_detail=job.job_type,
            )
            return
        finish_job(session_factory, job.id, worker_id, status="succeeded")
    except RevisionChanged as exc:
        # 快照已转 superseded；任务按可重试失败处理，重新领取后以新修订重建（§5.2）
        logger.info("job=%s 修订号变化，待重新构建：%s", job.id, exc)
        finish_job(
            session_factory,
            job.id,
            worker_id,
            status="failed",
            error_code="revision_changed",
            error_detail=str(exc),
        )
    except LeaseLost:
        logger.warning("job=%s 租约丢失，放弃执行", job.id)
    except AppError as exc:
        logger.warning("job=%s 业务失败：%s", job.id, exc.code)
        finish_job(
            session_factory,
            job.id,
            worker_id,
            status="failed",
            error_code=exc.code,
            error_detail=exc.message,
        )
    except Exception as exc:  # noqa: BLE001  兜底记录，任务不静默消失
        logger.exception("job=%s 执行异常", job.id)
        finish_job(
            session_factory,
            job.id,
            worker_id,
            status="failed",
            error_code="internal_error",
            error_detail=str(exc)[:500],
        )


async def run_worker_loop(*, once: bool = False, max_jobs: int | None = None) -> None:
    settings = get_settings()
    builder = IndexBuilder(
        SessionLocal, get_embedding_gateway(), settings, worker_id=WORKER_ID
    )
    processed = 0
    while max_jobs is None or processed < max_jobs:
        with SessionLocal() as session:
            recovered = recover_stuck_jobs(session)
            if recovered:
                logger.info("已标记 %s 个租约耗尽任务为失败", recovered)
            job = claim_next_job(session, WORKER_ID, settings.worker_lease_seconds)
        if job is None:
            if once:
                break
            await asyncio.sleep(settings.worker_poll_interval_seconds)
            continue
        logger.info("领取任务 job=%s type=%s attempt=%s", job.id, job.job_type, job.attempt)
        await execute_claimed(
            job,
            builder=builder,
            session_factory=SessionLocal,
            worker_id=WORKER_ID,
            lease_seconds=settings.worker_lease_seconds,
        )
        processed += 1
        if once:
            break


def main() -> None:
    from app.core.logging import configure_logging

    settings = get_settings()
    configure_logging(settings.log_level)
    logger.info("worker 启动：%s", WORKER_ID)
    try:
        asyncio.run(run_worker_loop())
    except KeyboardInterrupt:
        logger.info("worker 收到中断，退出")


if __name__ == "__main__":
    main()
