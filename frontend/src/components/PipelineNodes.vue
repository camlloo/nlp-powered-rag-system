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
  border: 2px dashed var(--ink);
  border-radius: var(--radius);
  background: var(--paper);
  padding: 8px 12px;
  margin-bottom: 10px;
  transform: rotate(-0.4deg);
  box-shadow: 2px 2px 0px var(--yellow);
}
.pipe-label-row { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.pipe-file { font-size: 12.5px; font-weight: 700; color: var(--ink); }
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
  font-weight: 700;
  color: var(--muted);
}
.pipe-icon { font-size: 12px; }
.pipe-node.done { color: var(--ink); }
.pipe-node.done .pipe-icon { color: var(--teal); }
.pipe-node.active { color: var(--ink); }
.pipe-node.active .pipe-icon { color: var(--red); animation: pipe-jitter 0.5s steps(2) infinite; }
.pipe-node.failed { color: var(--red); }
.pipe-node.failed .pipe-icon { color: var(--red); }
@keyframes pipe-jitter {
  0% { transform: rotate(-8deg) translateY(0); }
  50% { transform: rotate(8deg) translateY(-2px); }
  100% { transform: rotate(-5deg); }
}
.pipe-link {
  width: 18px;
  border-top: 2.5px dashed rgba(44, 44, 44, 0.35);
  flex-shrink: 0;
}
.pipe-link.filled { border-top-style: solid; border-top-color: var(--teal); }
</style>
