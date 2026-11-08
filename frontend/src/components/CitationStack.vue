<script setup>
import { ref, watch } from 'vue'

const props = defineProps({
  citations: { type: Array, default: () => [] },
})

const litId = ref(null)
let litTimer = null

function locate(citation) {
  litId.value = citation.citation_id
  clearTimeout(litTimer)
  litTimer = setTimeout(() => (litId.value = null), 1600)
}

defineExpose({ locate })

function onSealClick(citation) {
  locate(citation)
}

watch(
  () => props.citations,
  () => {
    litId.value = null
  }
)

const LOCATOR_TEXT = {
  pdf: (l) => `第 ${l.page} 页`,
  docx: (l) => `第 ${l.paragraph} 段${l.row ? ` · 表行 ${l.row}` : ""}`,
  txt: (l) => `行 ${l.line ?? "—"}`,
  markdown: (l) => `行 ${l.line ?? "—"}`,
}
function locatorText(locator) {
  if (!locator?.type) return ""
  const span = locator.spans?.[0] ?? {}
  const merged = { ...span, ...locator, ...span }
  return `${merged.type} · ${(LOCATOR_TEXT[merged.type] || (() => ""))(merged)}`
}
</script>

<template>
  <aside class="stack-col">
    <div class="panel stack-panel">
      <div class="panel-title">
        <span class="h-serif">引用卷宗</span>
        <span class="eyebrow">{{ citations.length }}</span>
      </div>
      <div class="panel-body stack-body">
        <div v-if="!citations.length" class="empty" aria-hidden="true">
          <svg viewBox="0 0 72 84" width="72" height="84" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" style="display:block;margin:0 auto 8px">
            <rect x="10" y="8" width="52" height="68" rx="4" />
            <path d="M22 26h28M22 38h28M22 50h18" opacity=".5" />
          </svg>
          引用将随回答出现
        </div>

        <article
          v-for="c in citations"
          :key="c.citation_id"
          class="cite-card"
          :class="{ lit: litId === c.citation_id }"
        >
          <header class="cite-card-head">
            <button class="seal" :title="`定位 ${c.source_id}`" @click="onSealClick(c)">
              {{ c.source_id.replace('S', '') }}
            </button>
            <div class="cite-doc">
              <span class="cite-title h-serif">{{ c.document_title }}</span>
              <span class="cite-loc">{{ locatorText(c.locator) }} · v{{ c.document_version }}</span>
            </div>
          </header>
          <blockquote class="cite-quote h-serif">「{{ c.quote }}」</blockquote>
        </article>
      </div>
    </div>
  </aside>
</template>

<style scoped>
.stack-col { overflow-y: auto; min-height: 0; }
.stack-panel { height: 100%; display: flex; flex-direction: column; }
.stack-body { overflow-y: auto; flex: 1; }

/* 卡片内为平铺列表块（不嵌套卡片），悬停仅底色变化 */
.cite-card {
  border-radius: var(--radius);
  padding: 10px 10px 10px 26px; /* 左侧预留拖拽手柄位 */
  margin-bottom: 4px;
  position: relative;
  background: transparent;
  transition: background var(--t);
}
/* Drag Handle Illusion */
.cite-card::before {
  content: '⋮⋮';
  position: absolute;
  left: 8px;
  top: 12px;
  font-size: 12px;
  line-height: 1;
  letter-spacing: -3px;
  color: var(--text-3);
  opacity: 0;
  transition: opacity var(--t);
}
.cite-card:hover { background: var(--hover); }
.cite-card:hover::before { opacity: 1; }
.cite-card.lit { background: var(--blue-bg-strong); }
.cite-card-head { display: flex; align-items: center; gap: 10px; }
.cite-card .seal {
  width: 26px;
  height: 26px;
  font-size: 13px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 0;
  flex-shrink: 0;
}
.cite-doc { display: flex; flex-direction: column; min-width: 0; }
.cite-title { font-size: 14px; color: var(--text); font-weight: 600; }
.cite-loc { font-size: 11px; color: var(--text-3); }
.cite-quote {
  margin: 8px 0 0;
  font-size: 13.5px;
  color: var(--text);
  background: var(--warn-bg); /* 柔和引文高亮 */
  padding: 1px 4px;
  border-radius: var(--radius-sm);
  display: inline;
  line-height: 1.9;
}
</style>
