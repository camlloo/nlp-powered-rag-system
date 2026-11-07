"""网关层错误：统一继承 AppError，携带稳定业务码（实现文档 §6.2/§9.4）。

- EmbeddingUnavailable：查询向量超时/不可用 -> 检索侧降级 BM25 并标识 lexical_only；
- GenerationUnavailable：生成失败 -> 返回已检索来源，不把错误伪装成答案；
- 协议校验与预算类错误不重试。
"""

from app.core.errors import AppError


class EmbeddingUnavailable(AppError):
    status_code = 503
    code = "embedding_unavailable"
    default_message = "向量服务暂不可用"
    retryable = True


class GenerationUnavailable(AppError):
    status_code = 503
    code = "generation_unavailable"
    default_message = "生成服务暂不可用"
    retryable = True


class ModelAuthError(AppError):
    status_code = 503
    code = "model_auth_error"
    default_message = "模型凭证或服务地址配置错误"
    retryable = False


class ModelInputTooLong(AppError):
    status_code = 422
    code = "model_input_too_long"
    default_message = "模型输入超过字节预算"
    retryable = False


class ModelBudgetExceeded(AppError):
    status_code = 429
    code = "model_budget_exceeded"
    default_message = "模型调用预算已用尽"
    retryable = False


class ModelResponseInvalid(AppError):
    """模型响应违反协议：索引缺项/乱序、维度不符、NaN、零向量等。"""

    status_code = 502
    code = "model_response_invalid"
    default_message = "模型响应不符合协议"
    retryable = False


class GenerationIncomplete(AppError):
    """finish_reason 非 stop 或内容为空（§8.3：业务层可重生成一次）。"""

    status_code = 502
    code = "generation_incomplete"
    default_message = "生成结果不完整"
    retryable = False
