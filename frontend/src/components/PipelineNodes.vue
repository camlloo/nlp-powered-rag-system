<script setup>
const props = defineProps({
  job: { type: Object, required: true },
})

const NODES = [
  { key: 'parse', label: '解析' },
  { key: 'chunk', label: '切块' },
  { key: 'embed', label: '向量化' },
  { key: 'validate', label: '校验' },
  { key: 'publish', label: '发布' },
]
const STAGE_INDEX = Object.fromEntries(NODES.map((n, i) => [n.key, i]))

function nodeState(key) {
  const job = props.job
  if (job.status === 'succeeded') return 'done'
  if (job.status === 'failed' || job.status === 'cancelled') {
    const failedIdx = STAGE_INDEX[job.stage ?? 'parse']
    const idx = STAGE_INDEX[key]
    if (idx < failedIdx) return 'done'
    if (idx === failedIdx) return 'failed'
    return 'idle'
  }
  if (job.status === 'queued') return 'idle'
  const idx = STAGE_INDEX[key]
  const cur = STAGE_INDEX[job.stage ?? 'parse']
  if (idx < cur) return 'done'
  if (idx === cur) return 'active'
  return 'idle'
}

const ICON = { done: '✓', failed: '✕', active: '●', idle: '○' }
const STATUS_BADGE = {
  succeeded: 'badge-ok',
  failed: 'badge-bad',
  running: 'badge-run',
  queued: '',
}
const STATUS_TEXT = {
  succeeded: '完成',
  failed: '失败',
  running: '处理中',
  queued: '排队中',
  cancelled: '已取消',
}
</script>

<template>
  <div class="pipe-wrap">
    <div class="pipe-label-row">
      <span class="pipe-file">{{ job.label }}</span>
      <span class="badge" :class="STATUS_BADGE[job.status] || ''">
        {{ STATUS_TEXT[job.status] || job.status }}
      </span>
      <span v-if="job.errorCode" class="badge badge-bad">{{ job.errorCode }}</span>
    </div>
    <div class="pipe">
      <template v-for="(node, i) in NODES" :key="node.key">
        <span class="pipe-node" :class="nodeState(node.key)">
          <span class="pipe-icon">{{ ICON[nodeState(node.key)] }}</span>
          <span class="pipe-label">{{ node.label }}</span>
        </span>
        <span
          v-if="i < NODES.length - 1"
          class="pipe-link"
          :class="{ filled: nodeState(node.key) === 'done' }"
        ></span>
      </template>
    </div>
  </div>
</template>

<style scoped>
.pipe-wrap {
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-alt);
  padding: 8px 12px;
  margin-bottom: 8px;
}
.pipe-label-row { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.pipe-file { font-size: 12.5px; font-weight: 600; color: var(--text); }
.pipe {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.pipe-node {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  font-weight: 500;
  color: var(--text-3);
}
.pipe-icon { font-size: 12px; }
.pipe-node.done { color: var(--text-2); }
.pipe-node.done .pipe-icon { color: var(--success); }
.pipe-node.active { color: var(--text); }
/* 进行中：透明度慢脉冲（reduced-motion 时由全局规则关闭） */
.pipe-node.active .pipe-icon { color: var(--blue); animation: node-pulse 1.2s ease-in-out infinite; }
@keyframes node-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.35; }
}
.pipe-node.failed { color: var(--danger-strong); }
.pipe-node.failed .pipe-icon { color: var(--danger); }
.pipe-link {
  width: 18px;
  height: 1px;
  background: var(--border);
  flex-shrink: 0;
}
.pipe-link.filled { background: var(--success); }
</style>
