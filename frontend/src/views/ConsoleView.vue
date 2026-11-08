<script setup>
import { onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { askStream, api } from '../api.js'
import AskPanel from '../components/AskPanel.vue'
import CitationStack from '../components/CitationStack.vue'
import { handleError, logout, refreshConversations, store } from '../store.js'

/* 任务视图：?id= 打开历史会话（审计行还原），无 id 为新建草稿。
   首次提问时自动创建会话并写入 URL，之后每轮问答归入该会话。 */
const route = useRoute()
const router = useRouter()

const messages = ref([])
const citations = ref([])
const busy = ref(false)
const askError = ref(null)
const conversationId = ref(null)
const convTitle = ref('新任务')
let askController = null

function resetConversation() {
  askController?.abort()
  askController = null
  messages.value = []
  citations.value = []
  askError.value = null
  busy.value = false
  conversationId.value = null
  convTitle.value = '新任务'
}

/* 审计消息 → 前端消息对象 */
function toMessage(m) {
  if (m.role === 'user') return { role: 'user', content: m.content }
  const interrupted = ['generation_failed', 'stale_index'].includes(m.status)
  return {
    role: 'assistant',
    streaming: false,
    status: m.status,
    answer: m.answer || '',
    plainAnswer: (m.answer || '').replace(/\[S\d+\]/g, ''),
    citations: m.citations || [],
    requestId: m.request_id || '',
    mode: '',
    error: interrupted ? `回答中断（${m.error_code || m.status}）` : null,
  }
}

async function openConversation(id) {
  resetConversation()
  conversationId.value = id
  try {
    const detail = await api.getConversation(id)
    convTitle.value = detail.title
    messages.value = detail.messages.map(toMessage)
    const last = [...messages.value].reverse().find((m) => m.role === 'assistant')
    if (last) citations.value = last.citations
  } catch (err) {
    if (err.status === 404) {
      resetConversation()
      router.replace({ path: '/console' })
    }
    handleError(err)
  }
}

/* query.id 变化（侧栏点击/首问写入）或「新建任务」信号 → 加载/重置 */
watch(
  () => [route.query.id, store.taskNonce],
  () => {
    const id = Number(route.query.id)
    if (id && id !== conversationId.value) openConversation(id)
    else if (!id && route.path === '/console' && (conversationId.value || messages.value.length)) resetConversation()
  },
  { immediate: true }
)

async function onAsk(question) {
  if (busy.value) return
  askError.value = null

  // 新任务首问：先建会话（标题=首个问题），URL 补 id（query 变化不重挂载）
  if (!conversationId.value) {
    try {
      const conv = await api.createConversation(question.slice(0, 120))
      conversationId.value = conv.conversation_id
      convTitle.value = conv.title
      await router.replace({ path: '/console', query: { id: conv.conversation_id } })
      await refreshConversations().catch(handleError)
    } catch (err) {
      handleError(err)
      return
    }
  }

  const controller = new AbortController()
  askController = controller
  const rounds = Number(store.settings.historyRounds) || 0
  const history = rounds > 0
    ? messages.value
      .filter((m) => m.role === 'user' || (m.status === 'answered' && m.plainAnswer))
      .slice(-rounds * 2)
      .map((m) => ({ role: m.role, content: (m.role === 'user' ? m.content : m.plainAnswer).slice(0, 2000) }))
    : []
  messages.value.push({ role: 'user', content: question })
  messages.value.push({
    role: 'assistant',
    streaming: true,
    status: 'answering',
    answer: '',
    plainAnswer: '',
    citations: [],
    requestId: '',
    mode: '',
    error: null,
  })
  const assistant = messages.value[messages.value.length - 1]

  try {
    await askStream(
      {
        question,
        history: history.filter((h) => h.content),
        top_k: Number(store.settings.topK) || 6,
        conversation_id: conversationId.value,
      },
      {
        onMeta: (d) => { assistant.requestId = d.request_id },
        onCitations: (d) => { assistant.citations = d.citations },
        onDelta: (d) => { assistant.answer += d.text },
        onNotice: (d) => { assistant.error = d.message; assistant.errorCode = d.code },
        onDone: (d) => {
          assistant.streaming = false
          assistant.status = d.status
          assistant.answer = d.answer ?? assistant.answer
          assistant.plainAnswer = (assistant.answer || '').replace(/\[S\d+\]/g, '')
          assistant.requestId = d.request_id || assistant.requestId
          assistant.mode = d.mode || assistant.mode
          if (d.citations) assistant.citations = d.citations
          refreshConversations().catch(handleError)
        },
        onError: (d) => {
          assistant.streaming = false
          assistant.status = 'error'
          assistant.error = d.message
          assistant.errorCode = d.code
        },
      },
      controller.signal
    )
    if (askController === controller) citations.value = assistant.citations
  } catch (err) {
    if (controller.signal.aborted) return
    assistant.streaming = false
    assistant.status = 'error'
    assistant.error = err.message
    if (err.status === 401) {
      logout()
    } else {
      askError.value = err
    }
  } finally {
    if (askController === controller) {
      askController = null
      busy.value = false
    }
  }
}

async function onFeedback(requestId, rating) {
  await api.feedback(requestId, rating, null)
  const msg = messages.value.find((m) => m.requestId === requestId)
  if (msg) msg.feedbackDone = true
}

/* 朱砂章 → 引用卷宗定位 */
const stackRef = ref(null)
function onLitSeal(sourceId) {
  const citation = citations.value.find((c) => c.source_id === sourceId)
  if (citation) stackRef.value?.locate(citation)
}

onUnmounted(() => askController?.abort())
</script>

<template>
  <div class="task-layout">
    <section class="task-strip">
      <span class="h-serif task-heading">{{ convTitle }}</span>
      <span v-if="conversationId" class="eyebrow">会话 #{{ conversationId }}</span>
      <span v-else class="eyebrow">新问题将自动创建任务</span>
    </section>
    <div class="chat-layout">
      <section class="chat-main">
        <div v-if="askError" class="error-note">{{ askError.message }}</div>
        <AskPanel
          :messages="messages"
          :busy="busy"
          :auto-debug="store.settings.autoDebug"
          @ask="onAsk"
          @feedback="(requestId, rating) => onFeedback(requestId, rating).catch(handleError)"
          @lit-seal="onLitSeal"
        />
      </section>
      <CitationStack ref="stackRef" :citations="citations" />
    </div>
  </div>
</template>

<style scoped>
.task-layout { display: flex; flex-direction: column; gap: 10px; }
.task-strip {
  display: flex;
  align-items: baseline;
  gap: 10px;
  padding: 0 2px;
}
.task-heading { font-size: 16px; max-width: 60%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.chat-layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(300px, 380px);
  gap: 14px;
  height: calc(100vh - 52px - 76px);
  min-height: 420px;
}
.chat-main { overflow-y: auto; min-width: 0; padding-bottom: 14px; }

@media (max-width: 1100px) {
  .chat-layout { grid-template-columns: 1fr; height: auto; }
  .chat-layout > :last-child { display: none; }
}
</style>
