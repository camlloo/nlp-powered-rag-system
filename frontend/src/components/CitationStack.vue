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
        <div v-if="!citations.length" class="empty-doodle" aria-hidden="true">
          <svg viewBox="0 0 120 100" width="120" height="100" fill="none" stroke="var(--teal)" stroke-width="2.5" stroke-linecap="round">
            <path d="M14 78 C 40 60, 62 44, 96 20" />
            <path d="M96 20 l-14 2 M96 20 l-4 13" stroke="var(--red)" />
            <path d="M20 24 l6 -8 6 8 8 -10" stroke="var(--red)" />
            <path d="M28 92 q 8 -6 16 0 t 16 0" stroke="var(--ink)" />
          </svg>
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
.stack-col { overflow-y: auto; min-height: 0; transform: rotate(0.3deg); }
.stack-panel { height: 100%; display: flex; flex-direction: column; }
.stack-body { overflow-y: auto; flex: 1; }
.empty-doodle { display: flex; justify-content: center; padding: 36px 0; transform: rotate(-2deg); }

.cite-card {
  border: 2px dashed var(--ink);
  border-radius: var(--radius);
  background: var(--paper);
  padding: 12px 12px 10px;
  margin-bottom: 16px;
  position: relative;
  transform: rotate(-0.6deg);
  box-shadow: 3px 3px 0px var(--yellow);
  transition: all 0.2s ease-in-out;
}
.cite-card:nth-child(2n) { transform: rotate(0.5deg); box-shadow: 3px 3px 0px var(--red); }
/* 图钉装饰 */
.cite-card::before {
  content: '';
  position: absolute;
  top: -7px;
  left: 16px;
  width: 13px;
  height: 13px;
  border-radius: 50%;
  background: var(--red);
  border: 2px dashed var(--ink);
}
.cite-card.lit {
  border-color: var(--red);
  background: rgba(255, 217, 51, 0.28);
  box-shadow: 4px 4px 0px var(--red);
  transform: rotate(0deg) scale(1.02);
}
.cite-card-head { display: flex; align-items: center; gap: 10px; }
.cite-card .seal { width: 30px; height: 30px; font-size: 14px; }
.cite-doc { display: flex; flex-direction: column; min-width: 0; }
.cite-title { font-size: 14px; color: var(--ink); }
.cite-loc { font-size: 11px; color: var(--muted); }
.cite-quote {
  margin: 8px 0 0;
  font-size: 13.5px;
  color: var(--ink);
  background: rgba(255, 217, 51, 0.3); /* 荧光笔划句子 */
  padding: 2px 6px;
  border-radius: var(--radius);
  display: inline;
}
</style>
