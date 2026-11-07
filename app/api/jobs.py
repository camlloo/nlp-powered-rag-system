"""任务状态接口（实现文档 §9.2/§9.4）。"""

from fastapi import APIRouter

from app.api.deps import PrincipalDep, SessionDep, require_kb_access
from app.core.errors import NotFound
from app.db.models import IngestJob
from app.schemas.api import JobStatusResponse

router = APIRouter()


@router.get("/jobs/{job_id}", response_model=JobStatusResponse)
def get_job(job_id: int, session: SessionDep, principal: PrincipalDep) -> JobStatusResponse:
    job = session.get(IngestJob, job_id)
    if job is None:
        raise NotFound("任务不存在")
    # 放行：管理员 ∨ KB 成员 ∨ 任务发起者（payload.uploader_user_id，
    # 去知识库化后上传者通常没有主空间成员身份）
    if not principal.can_see_all():
        actor_id = (job.payload_json or {}).get("uploader_user_id")
        if actor_id != principal.user_id:
            require_kb_access(kb_id=job.kb_id, session=session, principal=principal)
    return JobStatusResponse(
        job_id=job.id,
        kb_id=job.kb_id,
        doc_version_id=job.doc_version_id,
        job_type=job.job_type,
        status=job.status,
        stage=job.stage,
        attempt=job.attempt,
        max_attempt=job.max_attempt,
        error_code=job.error_code,
        error_detail=job.error_detail,
    )
