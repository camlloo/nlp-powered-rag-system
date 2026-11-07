<script setup>
import { reactive } from 'vue'
import { saveSettings, store } from '../store.js'

/* 系统设置：检索参数（本地生效） + 当前用户 */
const local = reactive({
  topK: store.settings.topK,
  historyRounds: store.settings.historyRounds,
  autoDebug: store.settings.autoDebug,
})

function submit() {
  saveSettings({
    topK: Number(local.topK) || 6,
    historyRounds: Number(local.historyRounds) || 0,
    autoDebug: Boolean(local.autoDebug),
  })
  store.notice = '设置已保存。'
}
</script>

<template>
  <section class="panel settings-panel">
    <div class="panel-title">
      <span class="h-serif">检索与回答</span>
      <span class="eyebrow">仅影响本浏览器</span>
    </div>
    <form class="panel-body" @submit.prevent="submit">
      <label class="fld">
        <span class="fld-label">回答引用的证据数量（1–10）</span>
        <input v-model.number="local.topK" type="number" min="1" max="10" />
      </label>
      <label class="fld">
        <span class="fld-label">追问时携带的历史轮数（0–3）</span>
        <input v-model.number="local.historyRounds" type="number" min="0" max="3" />
      </label>
      <label class="fld-check">
        <input v-model="local.autoDebug" type="checkbox" />
        <span>每次回答后自动展开检索调试</span>
      </label>

      <div class="foot">
        <button class="btn" type="submit">保存设置</button>
      </div>
    </form>

    <div v-if="store.currentUser" class="panel-title user-strip">
      <span class="h-serif">当前用户</span>
      <span class="user-info">
        <b>{{ store.currentUser.display_name }}</b>
        <span class="muted">@{{ store.currentUser.username }}</span>
        <span v-if="store.currentUser.org_tags?.length" class="muted">
          {{ store.currentUser.org_tags.join(' / ') }}
        </span>
      </span>
    </div>
  </section>
</template>

<style scoped>
.settings-panel { max-width: 640px; }
.fld { display: block; margin-bottom: 12px; }
.fld-label {
  display: block;
  font-size: 13px;
  font-weight: 700;
  color: var(--ink);
  margin-bottom: 5px;
}
.fld-check {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: var(--ink);
  cursor: pointer;
}
.fld-check input { accent-color: var(--red); }
.foot { display: flex; justify-content: flex-end; padding-top: 6px; }

.user-strip { justify-content: flex-start; gap: 10px; border-top: 2px dashed var(--ink); }
.user-info { display: flex; gap: 8px; align-items: baseline; font-size: 13px; }
.muted { color: var(--muted); font-size: 12px; }
</style>
