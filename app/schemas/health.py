"""健康检查响应模型。"""

from pydantic import BaseModel


class LivenessResponse(BaseModel):
    status: str


class ReadinessResponse(BaseModel):
    status: str
    database: str
    pgvector: str
    pg_version: str
