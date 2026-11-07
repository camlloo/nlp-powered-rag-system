/* 全局状态：响应式单例（不引 pinia，保持零额外依赖）。
   去知识库化：文档直接归属个人/组织，问答面向全部可见文档。
   对话状态（messages/citations）归属 ConsoleView 自身，不进这里。 */

import { computed, reactive } from 'vue'
import { api, clearTokens, hasToken, setJwt } from './api.js'

/* 会话版本号：登出/换登录后递增，使在途请求的结果作废 */
let sessionVersion = 0

/* ---------- 设置（先于 store 声明，模块初始化即调用） ---------- */
const SETTINGS_KEY = 'rag.settings'
const DEFAULT_SETTINGS = { topK: 6, historyRounds: 3, autoDebug: false }

function loadSettings() {
  try {
    return { ...DEFAULT_SETTINGS, ...JSON.parse(localStorage.getItem(SETTINGS_KEY) || '{}') }
  } catch {
    return { ...DEFAULT_SETTINGS }
  }
}

export const store = reactive({
  authenticated: hasToken(),
  currentUser: null,
  readyz: null,
  notice: '',

  docs: [],
  trackedJobs: [],

  // 任务列表（历史会话）
  conversations: [],
  conversationsTotal: 0,
  conversationsLoaded: 0,
  // 新建任务信号：AdminLayout 点击「新建任务」时递增，ConsoleView 监听重置
  taskNonce: 0,

  settings: loadSettings(),
})

export const isAdmin = computed(() => Boolean(store.currentUser?.is_admin))

export function saveSettings(payload) {
  store.settings = {
    topK: payload.topK,
    historyRounds: payload.historyRounds,
    autoDebug: payload.autoDebug,
  }
  localStorage.setItem(SETTINGS_KEY, JSON.stringify(store.settings))
}

/* ---------- 错误与会话防串 ---------- */
export function handleError(err) {
  if (err.status === 401) logout()
  else store.notice = err.message
}

export function logout() {
  sessionVersion += 1
  clearTokens()
  store.authenticated = false
  store.currentUser = null
  store.docs = []
  store.trackedJobs = []
  store.conversations = []
  store.conversationsTotal = 0
  store.conversationsLoaded = 0
  store.notice = ''
  window.location.hash = '#/login'
}

export async function authenticate(payload, register = false) {
  const authData = register ? await api.register(payload) : await api.login(payload.username, payload.password)
  clearTokens()
  setJwt(authData.access_token)
  sessionVersion += 1
  store.authenticated = true
  store.currentUser = authData.user
  return authData
}

export async function refreshMe() {
  const version = sessionVersion
  if (!hasToken()) {
    store.authenticated = false
    store.currentUser = null
    return
  }
  try {
    const user = await api.me()
    if (version !== sessionVersion) return
    store.currentUser = user
    store.authenticated = true
  } catch (err) {
    if (version === sessionVersion) handleError(err)
  }
}

/* ---------- 服务状态 ---------- */
export async function checkReady() {
  store.readyz = await api.readyz()
}

/* ---------- 文档与任务 ---------- */
export async function refreshDocs() {
  const version = sessionVersion
  const data = await api.listDocs()
  if (version === sessionVersion) store.docs = data.items
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const polling = new Set()

async function pollJob(jobId, onUpdate) {
  if (polling.has(jobId)) return
  polling.add(jobId)
  const version = sessionVersion
  try {
    for (let i = 0; i < 120; i++) {
      if (version !== sessionVersion) return
      const job = await api.job(jobId)
      if (version !== sessionVersion) return
      onUpdate?.(job)
      if (['succeeded', 'failed', 'cancelled'].includes(job.status)) {
        store.notice =
          job.status === 'failed'
            ? `任务失败：${job.error_code || '未知原因'}`
            : job.status === 'succeeded'
              ? '任务已完成。'
              : '任务已取消。'
        return
      }
      await sleep(1200)
    }
  } catch (err) {
    if (version === sessionVersion) handleError(err)
  } finally {
    polling.delete(jobId)
    if (version === sessionVersion) await refreshDocs().catch(handleError)
  }
}

export function trackJob(jobId, label) {
  store.trackedJobs = [
    { jobId, label, status: 'queued', stage: null, errorCode: null },
    ...store.trackedJobs,
  ].slice(0, 6)
  pollJob(jobId, (job) => {
    const entry = store.trackedJobs.find((t) => t.jobId === jobId)
    if (entry) {
      entry.status = job.status
      entry.stage = job.stage
      entry.errorCode = job.error_code
    }
  })
}

export async function uploadDoc(file, scope, visibility) {
  store.notice = ''
  const data = await api.uploadDoc(file, scope, visibility)
  if (data.duplicate) {
    store.notice = '该文件内容与已有文档相同。'
    await refreshDocs()
    return
  }
  store.notice = '已受理…'
  trackJob(data.job_id, file.name)
  await refreshDocs()
}

export async function deleteDoc(doc) {
  store.notice = ''
  const data = await api.deleteDoc(doc.document_id)
  trackJob(data.reindex_job_id, '删除后重建')
  await refreshDocs()
}

export async function updateDoc(doc, scope, visibility) {
  store.notice = ''
  const payload = {}
  if (scope !== null) payload.scope = scope
  if (visibility !== null) payload.visibility = visibility
  const updated = await api.updateDoc(doc.document_id, payload)
  Object.assign(doc, updated)
  store.notice = '文档属性已更新，检索可见范围即时生效。'
}

export async function reindexAll() {
  store.notice = ''
  const data = await api.reindexAll()
  trackJob(data.job_id, '全量重建')
}

/* ---------- 任务（历史会话）---------- */
const CONV_PAGE = 20

export async function refreshConversations() {
  const version = sessionVersion
  if (!hasToken()) return
  const data = await api.listConversations(CONV_PAGE, 0)
  if (version !== sessionVersion) return
  store.conversations = data.items
  store.conversationsTotal = data.total
  store.conversationsLoaded = data.items.length
}

export async function loadMoreConversations() {
  const version = sessionVersion
  const data = await api.listConversations(CONV_PAGE, store.conversationsLoaded)
  if (version !== sessionVersion) return
  store.conversations = [...store.conversations, ...data.items]
  store.conversationsTotal = data.total
  store.conversationsLoaded = store.conversations.length
}

export async function removeConversation(conversationId) {
  await api.deleteConversation(conversationId)
  store.conversations = store.conversations.filter((c) => c.conversation_id !== conversationId)
  store.conversationsTotal = Math.max(0, store.conversationsTotal - 1)
  store.conversationsLoaded = Math.max(0, store.conversationsLoaded - 1)
}

export function newTask() {
  store.taskNonce += 1
}
