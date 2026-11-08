/* API 客户端：同源 /api/v1，JWT 用户认证。
   去知识库化：文档直接归属「个人 / 组织」，问答面向全部可见文档。 */

const BASE = '/api/v1'
const JWT_KEY = 'rag.jwt'

export function getJwt() {
  return localStorage.getItem(JWT_KEY) || ''
}
export function setJwt(token) {
  localStorage.setItem(JWT_KEY, token)
}
export function hasToken() {
  return Boolean(getJwt())
}
export function clearTokens() {
  localStorage.removeItem(JWT_KEY)
  localStorage.removeItem('rag.token')
}

export class ApiError extends Error {
  constructor(status, code, message, requestId, retryable) {
    super(message)
    this.status = status
    this.code = code
    this.requestId = requestId
    this.retryable = retryable
  }
}

async function request(path, { method = 'GET', body, form, authenticated = true, stream = false, signal } = {}) {
  const headers = {}
  const jwt = getJwt()
  if (authenticated && jwt) headers['Authorization'] = `Bearer ${jwt}`
  if (!form) headers['Content-Type'] = 'application/json'
  let resp
  try {
    resp = await fetch(BASE + path, {
      method,
      headers,
      body: form ? form : body === undefined ? undefined : JSON.stringify(body),
      signal,
    })
  } catch (err) {
    if (signal?.aborted) throw err
    throw new ApiError(0, 'network_error', '无法连接服务——请确认 API 已启动。')
  }
  if (!resp.ok) {
    let code = 'http_error'
    let message = `请求失败（${resp.status}）`
    let requestId = ''
    let retryable = false
    try {
      const envelope = await resp.json()
      code = envelope.code ?? code
      message = envelope.message ?? message
      requestId = envelope.request_id ?? ''
      retryable = Boolean(envelope.retryable)
    } catch {
      /* 非 JSON 错误体，保留默认文案 */
    }
    throw new ApiError(resp.status, code, message, requestId, retryable)
  }
  if (resp.status === 204) return null
  return stream ? resp : resp.json()
}

export const api = {
  readyz: () => fetch('/readyz').then((r) => (r.ok ? r.json() : null)).catch(() => null),
  login: (username, password) =>
    request('/auth/login', {
      method: 'POST',
      body: { username, password },
      authenticated: false,
    }),
  register: (payload) =>
    request('/auth/register', {
      method: 'POST',
      body: payload,
      authenticated: false,
    }),

  me: () => request('/auth/me'),

  listDocs: (scope = 'all') => request(`/documents?limit=200&scope=${encodeURIComponent(scope)}`),
  uploadDoc: (file, scope = 'personal', visibility = 'private') => {
    const form = new FormData()
    form.append('file', file)
    form.append('scope', scope)
    form.append('visibility', visibility)
    return request('/documents', { method: 'POST', form })
  },
  deleteDoc: (documentId) => request(`/documents/${documentId}`, { method: 'DELETE' }),
  updateDoc: (documentId, payload) =>
    request(`/documents/${documentId}`, { method: 'PATCH', body: payload }),
  reindexAll: () => request('/admin/reindex', { method: 'POST', body: {} }),

  job: (jobId) => request(`/jobs/${jobId}`),

  // 任务/会话（历史对话）
  listConversations: (limit = 20, offset = 0) =>
    request(`/conversations?limit=${limit}&offset=${offset}`),
  createConversation: (title, signal) =>
    request('/conversations', { method: 'POST', body: { title }, signal }),
  getConversation: (conversationId, signal) => request(`/conversations/${conversationId}`, { signal }),
  deleteConversation: (conversationId) =>
    request(`/conversations/${conversationId}`, { method: 'DELETE' }),

  ask: (payload) => request('/qa', { method: 'POST', body: payload }),
  debug: (payload) => request('/retrieval/debug', { method: 'POST', body: payload }),
  feedback: (requestId, rating, reason) =>
    request('/feedback', { method: 'POST', body: { request_id: requestId, rating, reason } }),

  // 管理面：组织标签与用户管理（管理员专用，403 时由调用方提示）
  listOrgTags: () => request('/admin/org-tags'),
  createOrgTag: (name) => request('/admin/org-tags', { method: 'POST', body: { name } }),
  renameOrgTag: (tagId, name) =>
    request(`/admin/org-tags/${tagId}`, { method: 'PATCH', body: { name } }),
  deleteOrgTag: (tagId) => request(`/admin/org-tags/${tagId}`, { method: 'DELETE' }),
  listUsers: (limit = 200, offset = 0, filters = {}, signal) => {
    const query = new URLSearchParams({ limit, offset })
    if (filters.keyword) query.set('keyword', filters.keyword)
    if (filters.isAdmin !== null && filters.isAdmin !== undefined) {
      query.set('is_admin', String(filters.isAdmin))
    }
    for (const tagId of filters.orgTagIds || []) query.append('org_tag_ids', tagId)
    return request(`/admin/users?${query}`, { signal })
  },
  deleteUser: (userId) => request(`/admin/users/${userId}`, { method: 'DELETE' }),
  updateUserAdmin: (userId, isAdmin) =>
    request(`/admin/users/${userId}`, { method: 'PATCH', body: { is_admin: isAdmin } }),
  updateUserTags: (userId, tagIds) =>
    request(`/admin/users/${userId}/org-tags`, { method: 'PUT', body: { tag_ids: tagIds } }),
}

/* SSE 流式问答：POST /qa/stream，解析 event/data 帧分发给回调 */
export async function askStream(payload, handlers = {}, signal) {
  const resp = await request('/qa/stream', {
    method: 'POST',
    body: payload,
    stream: true,
    signal,
  })
  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let completed = false

  const dispatch = (frame) => {
    let event = 'message'
    let data = ''
    for (const line of frame.split('\n')) {
      if (line.startsWith('event: ')) event = line.slice(7).trim()
      else if (line.startsWith('data: ')) data += line.slice(6)
    }
    if (!data) return
    let parsed
    try {
      parsed = JSON.parse(data)
    } catch {
      return
    }
    switch (event) {
      case 'meta': handlers.onMeta?.(parsed); break
      case 'citations': handlers.onCitations?.(parsed); break
      case 'delta': handlers.onDelta?.(parsed); break
      case 'notice': handlers.onNotice?.(parsed); break
      case 'done': completed = true; handlers.onDone?.(parsed); break
      case 'error': completed = true; handlers.onError?.(parsed); break
    }
  }

  try {
    while (true) {
      const { done, value } = await reader.read()
      buffer += done ? decoder.decode() : decoder.decode(value, { stream: true })
      buffer = buffer.replace(/\r\n/g, '\n')
      let idx
      while ((idx = buffer.indexOf('\n\n')) !== -1) {
        const frame = buffer.slice(0, idx)
        buffer = buffer.slice(idx + 2)
        dispatch(frame)
      }
      if (done) break
    }
    if (buffer.trim()) dispatch(buffer)
    if (!completed) throw new ApiError(0, 'stream_interrupted', '回答连接已中断，请重新提问。')
  } finally {
    reader.releaseLock()
  }
}
