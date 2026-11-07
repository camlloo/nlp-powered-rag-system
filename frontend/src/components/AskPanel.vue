<script setup>
import { nextTick, ref, watch } from 'vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import { api } from '../api.js'

const props = defineProps({
  messages: { type: Array, default: () => [] },
  busy: { type: Boolean, default: false },
  autoDebug: { type: Boolean, default: false },
})
const emit = defineEmits(['ask', 'feedback', 'lit-seal'])

const question = ref('')
const threadEl = ref(null)
const debugOpen = ref(false)
const debug = ref(null)
const debugBusy = ref(false)
const debugError = ref(null)
const copiedIdx = ref(null)

marked.setOptions({ breaks: true, gfm: true })

function submit() {
  const q = question.value.trim()
  if (!q || props.busy) return
  emit('ask', q)
  question.value = ''
}

/* 答案渲染：markdown → 消毒 → [Sn] 注入朱砂引注章 */
function renderAnswer(msg) {
  if (!msg.answer) return ''
  const raw = marked.parse(msg.answer)
  const clean = DOMPurify.sanitize(raw)
  const withSeals = clean.replace(
    /\[S(\d+)\]/g,
    '<span class="seal" data-sid="S$1" role="button" title="查看引注 S$1">$1</span>'
  )
  return withSeals + (msg.streaming ? '<span class="streaming-cursor">▌</span>' : '')
}

/* 朱砂章点击（事件委托，v-html 内的元素不走 Vue 事件） */
function onAnswerClick(event) {
  const seal = event.target.closest('.seal')
  if (seal?.dataset.sid) emit('lit-seal', seal.dataset.sid)
}

async function copyUser(msg, idx) {
  try {
    await navigator.clipboard.writeText(msg.content)
    copiedIdx.value = idx
    setTimeout(() => (copiedIdx.value = null), 1200)
  } catch {
    /* 剪贴板不可用时静默 */
  }
}

const STATUS_TEXT = {
  insufficient_evidence: '证据不足，已拒绝回答',
  clarification_required: '需要澄清指代对象',
  answer_validation_failed: '引用校验未通过',
}

const STATUS_NOTE = {
  insufficient_evidence:
    '当前没有足够依据回答该问题。可换个问法，或在「文档管理」补充相关文档。',
  clarification_required:
    '问题里用了指代词（如“它”“该产品”），但上一轮的指代对象不明确——请写出具体名称后再问。',
  answer_validation_failed: '',
}


/* ---------- 检索调试 ---------- */
async function loadDebug() {
  const lastQuestion = [...props.messages].reverse().find((m) => m.role === 'user')
  debugBusy.value = true
  debugError.value = null
  try {
    debug.value = await api.debug({
      question: lastQuestion ? lastQuestion.content : 'test',
      top_k: 6,
    })
  } catch (err) {
    debugError.value = err.message
  } finally {
    debugBusy.value = false
  }
}
watch(debugOpen, (open) => {
  if (open && !debug.value) loadDebug()
})

/* 消息增长（流式 delta）与换页时贴底滚动 */
watch(
  () => props.messages.map((m) => (m.answer || '').length).join(','),
  async () => {
    await nextTick()
    threadEl.value?.scrollTo({ top: threadEl.value.scrollHeight, behavior: 'auto' })
  }
)
watch(
  () => props.messages.length,
  async () => {
    if (props.autoDebug) {
      debugOpen.value = true
      await loadDebug()
    } else if (debugOpen.value) {
      await loadDebug()
    }
    await nextTick()
    threadEl.value?.scrollTo({ top: threadEl.value.scrollHeight, behavior: 'smooth' })
  }
)

</script>
<template>
  <div class="panel ask-panel">
    <div class="panel-title">
      <span class="h-serif">对话</span>
    </div>

    <div ref="threadEl" class="thread panel-body">
      <div v-if="!messages.length" class="empty intro">
        <svg viewBox="0 0 140 90" width="140" height="90" fill="none" stroke-linecap="round" aria-hidden="true">
          <path d="M12 70 C 42 52, 70 38, 112 16" stroke="var(--teal)" stroke-width="2.5" />
          <path d="M112 16 l-15 3 M112 16 l-4 14" stroke="var(--red)" stroke-width="2.5" />
          <path d="M18 22 l6 -9 6 9 8 -11" stroke="var(--red)" stroke-width="2.5" />
          <path d="M96 62 q 9 -7 18 0 t 18 0" stroke="var(--ink)" stroke-width="2" />
          <circle cx="30" cy="46" r="3" stroke="var(--yellow)" stroke-width="2.5" fill="rgba(255,217,51,.5)" />
        </svg>
      </div>

      <div v-for="(msg, i) in messages" :key="i" class="msg" :class="`msg-${msg.role}`">
        <!-- 用户：右侧浅蓝胶囊 + 悬停复制 -->
        <template v-if="msg.role === 'user'">
          <div class="bubble-user">{{ msg.content }}</div>
          <div class="user-actions">
            <button class="act-btn" @click="copyUser(msg, i)">
              {{ copiedIdx === i ? '已复制' : '复制' }}
            </button>
          </div>
        </template>

        <!-- 助手：无卡片平铺正文（markdown 渲染 + 朱砂章） -->
        <template v-else-if="msg.role === 'assistant'">
          <div
            v-if="msg.answer && (msg.status === 'answered' || msg.streaming)"
            class="answer-md"
            @click="onAnswerClick"
            v-html="renderAnswer(msg)"
          ></div>

          <div v-if="msg.status === 'answering' && !msg.answer" class="status-note">
            <strong>检索中…</strong>
          </div>

          <div v-else-if="STATUS_TEXT[msg.status]" class="status-note">
            <strong>{{ STATUS_TEXT[msg.status] }}</strong>
            <span v-if="STATUS_NOTE[msg.status]">{{ STATUS_NOTE[msg.status] }}</span>
          </div>

          <p v-if="msg.error" class="error-note">{{ msg.error }}</p>

          <div v-if="msg.citations?.length" class="cite-strip">
            <span v-for="c in msg.citations" :key="c.citation_id" class="cite-chip">
              {{ c.source_id }} {{ c.document_title }}·v{{ c.document_version }}
            </span>
          </div>

          <div v-if="!msg.streaming && msg.requestId && msg.status === 'answered'" class="assistant-meta">
            <template v-if="!msg.feedbackDone">
              <button class="act-btn" @click="emit('feedback', msg.requestId, 'helpful')">有帮助</button>
              <button class="act-btn" @click="emit('feedback', msg.requestId, 'unhelpful')">没帮助</button>
            </template>
            <span v-else class="fb-done">已记录反馈</span>
          </div>
        </template>

        <div v-else class="error-note">{{ msg.content }}</div>
      </div>

      <div v-if="busy" class="thinking">
        <span class="dot badge-run" style="width: 6px; height: 6px"></span>
        检索中…
      </div>
    </div>

    <!-- 检索调试 -->
    <details class="debug" :open="debugOpen" @toggle="(e) => ((debugOpen = e.target.open), debugOpen && loadDebug())">
      <summary>检索调试</summary>
      <div class="debug-body">
        <p v-if="debugError" class="error-note">{{ debugError }}</p>
        <p v-if="debugBusy" class="empty">加载中…</p>
        <template v-if="debug">
          <div class="dbg-meta">
            terms: {{ debug.terms.join(' / ') || '—' }}
          </div>
          <div class="dbg-cols">
            <div class="dbg-col">
              <div class="dbg-head"><span class="dot" style="background: var(--amber)"></span> 词项通道</div>
              <ol class="dbg-list">
                <li v-for="h in debug.lexical.slice(0, 8)" :key="h.chunk_id">
                  <span class="cid">#{{ h.chunk_id }}</span>
                  <span class="score">{{ h.score.toFixed(4) }}</span>
                </li>
              </ol>
            </div>
            <div class="dbg-col">
              <div class="dbg-head"><span class="dot" style="background: var(--teal)"></span> 向量通道</div>
              <ol class="dbg-list">
                <li v-for="h in debug.vector.slice(0, 8)" :key="h.chunk_id">
                  <span class="cid">#{{ h.chunk_id }}</span>
                  <span class="score">{{ h.cosine?.toFixed(4) }}</span>
                </li>
              </ol>
            </div>
            <div class="dbg-col">
              <div class="dbg-head"><span class="dot" style="background: var(--ink)"></span> 融合排序</div>
              <ol class="dbg-list">
                <li v-for="h in debug.fused.slice(0, 8)" :key="h.chunk_id">
                  <span class="cid">#{{ h.chunk_id }}</span>
                  <span class="score">{{ h.score.toFixed(4) }}</span>
                </li>
              </ol>
            </div>
          </div>
          <div class="dbg-gate">
            gate: {{ debug.status }} · 余弦 {{ debug.gate?.top_cosine ?? '—' }} · 覆盖 {{ debug.gate?.keyword_coverage ?? '—' }}
          </div>
        </template>
      </div>
    </details>

    <!-- 提问 -->
    <form class="ask-form" @submit.prevent="submit">
      <textarea
        v-model="question"
        rows="2"
        maxlength="2000"
        placeholder="输入你的问题"
        @keydown.enter.exact.prevent="submit"
      ></textarea>
      <button class="btn" type="submit" :disabled="busy || !question.trim()">
        {{ busy ? '检索中…' : '询问' }}
      </button>
    </form>
  </div>
</template>

<style scoped>
.ask-panel { display: flex; flex-direction: column; min-height: 0; height: 100%; }

/* 书写区：笔记本横线（与回答行高对齐） */
.thread {
  overflow-y: auto;
  flex: 1;
  min-height: 120px;
  background-image: repeating-linear-gradient(
    transparent,
    transparent 27px,
    rgba(78, 205, 196, 0.14) 27px,
    rgba(78, 205, 196, 0.14) 28px
  );
}

/* 对话节奏：大间距 */
.msg { margin: 28px 0; }
.msg-user { display: flex; flex-direction: column; align-items: flex-end; }
.bubble-user {
  background: rgba(255, 217, 51, 0.5); /* 荧光笔划重点 */
  border: 2px dashed var(--ink);
  border-radius: var(--radius);
  padding: 8px 14px;
  max-width: 72%;
  font-size: 14.5px;
  white-space: pre-wrap;
  word-break: break-word;
  transform: rotate(0.6deg);
}
.user-actions {
  margin-top: 5px;
  display: flex;
  gap: 6px;
  opacity: 0;
  transition: opacity 0.15s ease;
}
.msg-user:hover .user-actions { opacity: 1; }
.act-btn {
  border: none;
  background: transparent;
  color: var(--muted);
  font-family: var(--hand);
  font-size: 12px;
  padding: 2px 4px;
}
.act-btn:hover { color: var(--red); text-decoration: underline wavy var(--red) 1px; text-underline-offset: 3px; }

/* 回答：平铺正文（手写感标题字体仅用于强调） */
.answer-md {
  font-size: 15px;
  line-height: 1.85;
  color: var(--ink);
  word-break: break-word;
}
.answer-md :deep(p) { margin: 0 0 10px; }
.answer-md :deep(p:last-child) { margin-bottom: 0; }
.answer-md :deep(ul), .answer-md :deep(ol) { margin: 4px 0 12px; padding-left: 22px; }
.answer-md :deep(li) { margin: 4px 0; }
.answer-md :deep(strong) {
  color: var(--ink);
  background: rgba(255, 217, 51, 0.45); /* 荧光笔强调 */
  padding: 0 3px;
}
.answer-md :deep(code) {
  font-family: var(--sans);
  font-size: 13px;
  background: rgba(78, 205, 196, 0.22);
  border: 1.5px dashed var(--ink);
  border-radius: var(--radius);
  padding: 1px 5px;
}
.answer-md :deep(pre) {
  background: var(--paper);
  border: 2px dashed var(--ink);
  border-radius: var(--radius);
  padding: 10px 12px;
  overflow-x: auto;
  margin: 8px 0 12px;
  box-shadow: 3px 3px 0px var(--yellow);
}
.answer-md :deep(pre code) { background: transparent; border: none; padding: 0; }
.answer-md :deep(h1), .answer-md :deep(h2), .answer-md :deep(h3) {
  font-family: var(--hand);
  transform: rotate(-0.4deg);
  margin: 14px 0 8px;
}
.answer-md :deep(blockquote) {
  border: 2px dashed var(--teal);
  border-radius: var(--radius);
  margin: 8px 0;
  padding: 4px 12px;
  color: var(--ink);
  background: rgba(78, 205, 196, 0.12);
}
.answer-md :deep(.seal) { vertical-align: 1px; }

.streaming-cursor { color: var(--red); font-weight: 700; animation: cursor-jitter 0.5s steps(2) infinite; }
@keyframes cursor-jitter {
  0% { transform: rotate(-4deg) translateY(0); }
  50% { transform: rotate(3deg) translateY(-1px); opacity: 0.5; }
  100% { transform: rotate(-2deg); opacity: 1; }
}

.status-note { font-size: 14px; }
.status-note strong { display: block; transform: rotate(-0.3deg); }
.status-note span { color: var(--muted); font-size: 13px; }

.cite-strip {
  margin-top: 12px;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}
.cite-chip {
  border: 1.5px dashed var(--ink);
  padding: 1px 7px;
  border-radius: var(--radius);
  font-size: 11px;
  background: rgba(255, 217, 51, 0.25);
  transform: rotate(-0.5deg);
}
.cite-chip:nth-child(2n) { transform: rotate(0.7deg); }

.assistant-meta {
  margin-top: 10px;
  display: flex;
  align-items: center;
  gap: 12px;
  opacity: 0;
  transition: opacity 0.15s ease;
}
.msg-assistant:hover .assistant-meta { opacity: 1; }
.fb-done { color: var(--teal); font-family: var(--hand); font-size: 12px; }

.thinking { font-size: 12px; color: var(--muted); display: flex; align-items: center; gap: 6px; }

/* 检索调试 */
.debug { border-top: 2px dashed var(--ink); background: var(--paper-alt); }
.debug summary {
  padding: 8px 14px;
  font-size: 12px;
  font-weight: 700;
  color: var(--muted);
  cursor: pointer;
}
.debug summary:hover { color: var(--red); }
.debug-body { padding: 4px 14px 12px; }
.dbg-meta { font-size: 12px; color: var(--muted); margin-bottom: 8px; }
.dbg-cols { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }
.dbg-head { font-size: 12px; font-weight: 700; display: flex; align-items: center; gap: 6px; margin-bottom: 4px; color: var(--ink); }
.dbg-list { list-style: none; margin: 0; padding: 0; font-size: 12px; }
.dbg-list li { display: flex; justify-content: space-between; padding: 1px 0; border-bottom: 1.5px dashed rgba(44, 44, 44, 0.2); }
.cid { color: var(--red); font-weight: 700; }
.score { color: var(--ink); }
.dbg-gate { margin-top: 8px; font-size: 12px; color: var(--muted); }

/* 提问表单 */
.ask-form {
  border-top: 2px dashed var(--ink);
  padding: 12px 14px;
  display: flex;
  gap: 10px;
  align-items: flex-end;
}
.ask-form textarea { flex: 1; }
.ask-form .btn { height: 40px; min-width: 88px; }
</style>
