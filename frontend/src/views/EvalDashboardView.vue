<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '../api.js'

/* 模型评测（管理员）：只读展示 tests/eval_medical.py 最近一轮产物。
   数据缺失（轨道未跑）一律显示"未运行"，不报错。 */

const overview = ref(null)
const qaRows = ref(null)
const loading = ref(true)
const error = ref('')
const prefTab = ref('with_reference')
const expandedId = ref('')

onMounted(async () => {
  try {
    overview.value = await api.evalOverview()
  } catch (err) {
    error.value = err.message || '评测数据加载失败'
  }
  loading.value = false
  try {
    qaRows.value = (await api.evalDetails('qa')).rows
  } catch { /* 明细可缺省 */ }
})

function num(v, digits = 3) {
  return v === null || v === undefined ? '—' : Number(v).toFixed(digits)
}
function pct(v, digits = 1) {
  return v === null || v === undefined ? '—' : (Number(v) * 100).toFixed(digits) + '%'
}
function ms(v) {
  return v === null || v === undefined ? '—' : Math.round(Number(v)).toLocaleString() + ' ms'
}

const ov = () => overview.value || {}

const kpis = computed(() => {
  const d = ov()
  const rt = (d.retrieval || {}).huatuo || {}
  const perf = (d.system_perf || {})['qa./qa'] || {}
  return [
    { label: '检索 recall@1', sub: '华佗百科库', value: pct(rt['recall@1']) },
    { label: '开放问答正确分', sub: `二元分 · n=${(d.qa || {}).n ?? '—'}`, value: num((d.qa || {}).mean_score, 2) },
    { label: '忠实度', sub: 'RAGAS faithfulness', value: pct((d.ragas || {}).faithfulness, 1) },
    { label: 'MCQ 准确率', sub: `CMMLU · n=${(d.mcq || {}).n ?? '—'}`, value: pct((d.mcq || {}).overall_acc, 1) },
    { label: '答案-金标余弦', sub: '百炼 embedding', value: num((d.qa || {}).cosine_gold_mean) },
    { label: '/qa 平均响应', sub: `成功率 ${pct(perf.success_rate, 0)}`, value: ms(perf.avg_ms) },
  ]
})

const retrievalSources = computed(() => {
  const r = ov().retrieval || {}
  return [
    { key: 'huatuo', title: '华佗百科库（gold 互含判定）', s: r.huatuo },
    { key: 'cmedqa', title: 'cMedQA 论坛库（evidence 文本互含）', s: r.cmedqa },
  ]
})

const ragasItems = computed(() => {
  const g = ov().ragas || {}
  return [
    { label: 'faithfulness 回答忠于证据', value: g.faithfulness, note: '回答主张可被检索上下文支持的比例' },
    { label: 'answer_relevancy 回答相关度', value: g.answer_relevancy, note: '回答与问题的语义相关（embedding）' },
    { label: 'context_precision（含参考）', value: g.llm_context_precision_with_reference, note: '上下文相对参考答案的精准度' },
    { label: 'context_recall 要点覆盖', value: g.context_recall, note: '参考答案要点在证据中的覆盖率' },
  ]
})

const prefAvailable = computed(() => {
  const p = (ov().preference) || {}
  return {
    with_reference: Boolean(p.with_reference),
    no_reference: Boolean(p.no_reference),
  }
})

const prefDims = computed(() => {
  const p = (ov().preference || {})[prefTab.value]
  if (!p || !p.winrate_pct) return []
  return Object.entries(p.winrate_pct).map(([dim, w]) => ({ dim, ...w }))
})

const binaryDist = computed(() => {
  if (!qaRows.value) return null
  const dist = { 1: 0, 0.5: 0, 0: 0 }
  for (const r of qaRows.value) {
    if (r.score === 1) dist[1] += 1
    else if (r.score === 0.5) dist[0.5] += 1
    else dist[0] += 1
  }
  const total = qaRows.value.length || 1
  return [
    { label: '正确 (1)', n: dist[1], w: dist[1] / total, cls: 'seg-ok' },
    { label: '部分 (0.5)', n: dist[0.5], w: dist[0.5] / total, cls: 'seg-half' },
    { label: '错误/拒答 (0)', n: dist[0], w: dist[0] / total, cls: 'seg-bad' },
  ]
})

const mcq = computed(() => ov().mcq || null)

const perfRows = computed(() => {
  const p = ov().system_perf || {}
  return Object.entries(p).map(([name, v]) => ({ name, ...v }))
})

const fileBadges = computed(() => {
  const present = new Set((ov().meta || {}).files_present || [])
  const wanted = [
    'retrieval_cmedqa.json', 'retrieval_huatuo.json', 'qa_summary.json',
    'ragas_summary.json', 'mcq.jsonl', 'preference_summary.json',
    'preference_with_gold_summary.json', 'system_perf.json',
  ]
  return wanted.map((name) => ({ name, ok: present.has(name) }))
})

function dimBar(v) {
  return v === null || v === undefined ? 0 : Math.max(0, Math.min(5, Number(v))) / 5 * 100
}
</script>

<template>
  <div class="eval-page">
    <div v-if="loading" class="panel"><div class="panel-body muted">正在加载评测数据…</div></div>
    <div v-else-if="error" class="error-note">
      <span class="eyebrow">加载失败</span>{{ error }}
    </div>

    <template v-else>
      <!-- ① 核心概览 -->
      <section class="panel">
        <div class="panel-title">
          <span class="h-serif">核心指标</span>
          <span class="eyebrow">最近一轮评测 · 详见 docs/医疗问答系统评测报告_20261111.md</span>
        </div>
        <div class="kpi-grid">
          <div v-for="k in kpis" :key="k.label" class="kpi">
            <div class="kpi-value">{{ k.value }}</div>
            <div class="kpi-label">{{ k.label }}</div>
            <div class="eyebrow">{{ k.sub }}</div>
          </div>
        </div>
      </section>

      <!-- ② 检索效果 -->
      <section v-for="src in retrievalSources" :key="src.key" class="panel">
        <div class="panel-title">
          <span class="h-serif">检索 · {{ src.title }}</span>
          <span v-if="!src.s" class="badge">未运行</span>
          <span v-else class="eyebrow">n={{ src.s.n }} · 命中=evidence 集文本互含</span>
        </div>
        <div v-if="src.s" class="panel-body">
          <table class="admin-table">
            <thead>
              <tr><th>k</th><th class="num">Precision@k</th><th class="num">Recall@k</th><th class="num">F1@k</th></tr>
            </thead>
            <tbody>
              <tr v-for="k in [1, 3, 5, 10]" :key="k">
                <td>@{{ k }}</td>
                <td class="num">{{ num(src.s[`precision@${k}`]) }}</td>
                <td class="num">{{ num(src.s[`recall@${k}`]) }}</td>
                <td class="num">{{ num(src.s[`f1@${k}`]) }}</td>
              </tr>
            </tbody>
          </table>
          <div class="bar-list">
            <div v-for="k in [1, 3, 5, 10]" :key="'r' + k" class="bar-row">
              <span class="bar-name">recall@{{ k }}</span>
              <div class="bar-track"><div class="bar-fill b-blue" :style="{ width: (src.s[`recall@${k}`] || 0) * 100 + '%' }"></div></div>
              <span class="bar-val">{{ num(src.s[`recall@${k}`]) }}</span>
            </div>
          </div>
          <p class="muted note">
            MRR@10 {{ num(src.s['mrr@10']) }} · 向量通道 Top1 余弦均值 {{ num(src.s.cosine_top1_mean) }}
          </p>
        </div>
      </section>

      <!-- ③ 开放问答 -->
      <section class="panel">
        <div class="panel-title">
          <span class="h-serif">开放问答 · 五维裁判评分（0–5）</span>
          <span v-if="!ov().qa" class="badge">未运行</span>
          <span v-else class="eyebrow">n={{ ov().qa.n }} · 二元分均值 {{ num(ov().qa.mean_score, 2) }} · 金标余弦 {{ num(ov().qa.cosine_gold_mean) }}</span>
        </div>
        <div v-if="ov().qa" class="panel-body">
          <div class="bar-list">
            <div v-for="(v, dim) in ov().qa.dim_means_0_5" :key="dim" class="bar-row">
              <span class="bar-name">{{ dim }}</span>
              <div class="bar-track"><div class="bar-fill b-accent" :style="{ width: dimBar(v) + '%' }"></div></div>
              <span class="bar-val">{{ num(v, 2) }}</span>
            </div>
          </div>

          <div v-if="binaryDist" class="dist-wrap">
            <span class="eyebrow">二元分分布</span>
            <div class="dist-bar">
              <div v-for="seg in binaryDist" :key="seg.label" :class="seg.cls"
                   :style="{ width: seg.w * 100 + '%' }" :title="`${seg.label}：${seg.n} 题`"></div>
            </div>
            <div class="dist-legend">
              <span v-for="seg in binaryDist" :key="'l' + seg.label">
                <i :class="['dotseg', seg.cls]"></i>{{ seg.label }} × {{ seg.n }}
              </span>
            </div>
          </div>

          <div v-if="qaRows" class="qa-detail">
            <span class="eyebrow">逐题明细（点击行展开答案）</span>
            <table class="admin-table">
              <thead>
                <tr><th>问题</th><th class="num">分</th><th class="num">余弦</th><th>理由</th></tr>
              </thead>
              <tbody>
                <template v-for="r in qaRows" :key="r.id">
                  <tr class="clickable" @click="expandedId = expandedId === r.id ? '' : r.id">
                    <td class="q-cell">{{ r.question }}</td>
                    <td class="num">
                      <span class="badge" :class="r.score === 1 ? 'badge-ok' : r.score === 0.5 ? 'badge-warn' : 'badge-bad'">
                        {{ r.score }}
                      </span>
                    </td>
                    <td class="num">{{ num(r.cosine_gold) }}</td>
                    <td class="reason-cell">{{ (r.reason || '').slice(0, 46) }}</td>
                  </tr>
                  <tr v-if="expandedId === r.id" class="expand-row">
                    <td colspan="4">
                      <p class="muted">五维：{{ Object.entries(r.dims || {}).map(([k, v]) => `${k} ${v}`).join(' · ') || '—' }}</p>
                      <p class="answer-excerpt">{{ r.answer }}</p>
                    </td>
                  </tr>
                </template>
              </tbody>
            </table>
          </div>
        </div>
      </section>

      <!-- ④ RAGAS -->
      <section class="panel">
        <div class="panel-title">
          <span class="h-serif">RAGAS 归因指标</span>
          <span v-if="!ov().ragas" class="badge">未运行</span>
          <span v-else class="eyebrow">n={{ ov().ragas.n }} · 真实管线上下文</span>
        </div>
        <div v-if="ov().ragas" class="panel-body">
          <div class="bar-list">
            <div v-for="item in ragasItems" :key="item.label" class="bar-row">
              <span class="bar-name wide">{{ item.label }}</span>
              <div class="bar-track"><div class="bar-fill b-teal" :style="{ width: (item.value || 0) * 100 + '%' }"></div></div>
              <span class="bar-val">{{ num(item.value) }}</span>
            </div>
          </div>
          <p class="muted note">{{ ragasItems.map(i => i.note).join('；') }}</p>
        </div>
      </section>

      <!-- ⑤ 盲评胜出率 -->
      <section class="panel">
        <div class="panel-title">
          <span class="h-serif">盲评胜出率 · RAG 全管线 vs 裸模型</span>
          <span class="eyebrow">随机位次 · 裁判逐维选优（借鉴论文表 4-4~4-6）</span>
        </div>
        <div class="panel-body">
          <div class="tabs">
            <button class="tab" :class="{ on: prefTab === 'with_reference' }"
                    :disabled="!prefAvailable.with_reference" @click="prefTab = 'with_reference'">
              带参考答案
            </button>
            <button class="tab" :class="{ on: prefTab === 'no_reference' }"
                    :disabled="!prefAvailable.no_reference" @click="prefTab = 'no_reference'">
              无参考（论文口径）
            </button>
          </div>
          <p v-if="!prefDims.length" class="empty">该口径本轮未运行。</p>
          <div v-else class="bar-list">
            <div v-for="row in prefDims" :key="row.dim" class="bar-row">
              <span class="bar-name">{{ row.dim }}</span>
              <div class="tri-track">
                <div class="tri rag" :style="{ width: row.rag + '%' }" :title="`RAG ${row.rag}%`"></div>
                <div class="tri tie" :style="{ width: row.tie + '%' }" :title="`平局 ${row.tie}%`"></div>
                <div class="tri bare" :style="{ width: row.bare + '%' }" :title="`裸模型 ${row.bare}%`"></div>
              </div>
              <span class="bar-val tri-legend">
                <i class="sw sw-rag"></i>{{ row.rag }}%
                <i class="sw sw-tie"></i>{{ row.tie }}%
                <i class="sw sw-bare"></i>{{ row.bare }}%
              </span>
            </div>
            <p class="muted note">
              <span class="sw sw-rag"></span>RAG 全管线胜
              <span class="sw sw-tie"></span>平局
              <span class="sw sw-bare"></span>裸模型胜——带参考口径体现事实性优势，无参考口径受长文流畅度影响。
            </p>
          </div>
        </div>
      </section>

      <!-- ⑥ MCQ -->
      <section class="panel">
        <div class="panel-title">
          <span class="h-serif">医学选择题 · CMMLU 8 子集</span>
          <span v-if="!mcq" class="badge">未运行</span>
          <span v-else class="eyebrow">n={{ mcq.n }} · 整体 {{ pct(mcq.overall_acc) }}（给出选项 {{ mcq.answered }} 题中 {{ pct(mcq.answered_acc) }}）</span>
        </div>
        <div v-if="mcq" class="panel-body">
          <div class="bar-list">
            <div v-for="(v, subject) in mcq.by_subject" :key="subject" class="bar-row">
              <span class="bar-name wide">{{ subject }}</span>
              <div class="bar-track"><div class="bar-fill b-blue" :style="{ width: (v.correct / v.total) * 100 + '%' }"></div></div>
              <span class="bar-val">{{ v.correct }}/{{ v.total }}</span>
            </div>
          </div>
          <p class="muted note">{{ mcq.no_option }}/{{ mcq.n }} 题因证据不足未给出选项（拒答），按错误计入整体。</p>
        </div>
      </section>

      <!-- ⑦ 工程指标 -->
      <section v-if="perfRows.length" class="panel">
        <div class="panel-title">
          <span class="h-serif">性能与工程指标</span>
          <span class="eyebrow">客户端逐请求计时（成功率 / avg / min / max）</span>
        </div>
        <div class="panel-body">
          <table class="admin-table">
            <thead>
              <tr><th>调用</th><th class="num">成/败</th><th class="num">成功率</th><th class="num">平均</th><th class="num">最小</th><th class="num">最大</th></tr>
            </thead>
            <tbody>
              <tr v-for="row in perfRows" :key="row.name">
                <td>{{ row.name }}</td>
                <td class="num">{{ row.n_ok }}/{{ row.n_fail }}</td>
                <td class="num">
                  <span class="badge" :class="row.success_rate >= 0.99 ? 'badge-ok' : 'badge-warn'">{{ pct(row.success_rate, 0) }}</span>
                </td>
                <td class="num">{{ ms(row.avg_ms) }}</td>
                <td class="num">{{ ms(row.min_ms) }}</td>
                <td class="num">{{ ms(row.max_ms) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <!-- ⑧ 本轮说明 -->
      <section class="panel">
        <div class="panel-title">
          <span class="h-serif">本轮评测文件</span>
          <span class="eyebrow">tests/data/medical/results/ · 固定种子 42 可复现</span>
        </div>
        <div class="panel-body file-badges">
          <span v-for="f in fileBadges" :key="f.name" class="badge" :class="f.ok ? 'badge-ok' : 'badge-warn'">
            {{ f.name }}
          </span>
        </div>
      </section>
    </template>
  </div>
</template>

<style scoped>
.eval-page { display: flex; flex-direction: column; gap: 16px; padding-bottom: 40px; }

/* KPI 概览 */
.kpi-grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: 1px; background: var(--divider);
}
.kpi { background: var(--bg); padding: 14px 16px; }
.kpi-value { font-size: 22px; font-weight: 600; color: var(--text); letter-spacing: 0.2px; }
.kpi-label { font-size: 13px; color: var(--text-2); margin: 2px 0 4px; }

/* 条形图 */
.bar-list { display: flex; flex-direction: column; gap: 8px; margin-top: 12px; }
.bar-row { display: grid; grid-template-columns: 110px 1fr 240px; gap: 10px; align-items: center; }
.bar-row:has(.wide) { grid-template-columns: 210px 1fr 240px; }
.bar-name { font-size: 12.5px; color: var(--text-2); text-align: right; }
.bar-track { height: 10px; background: var(--neutral-bg); border-radius: 3px; overflow: hidden; }
.bar-fill { height: 100%; border-radius: 3px; }
.b-blue { background: var(--blue); }
.b-teal { background: var(--success); }
.b-accent { background: var(--accent); }
.bar-val { font-size: 12.5px; color: var(--text); font-variant-numeric: tabular-nums; }

/* 三段胜出率 */
.tri-track { display: flex; height: 10px; border-radius: 3px; overflow: hidden; background: var(--neutral-bg); }
.tri { height: 100%; }
.tri.rag { background: var(--blue); }
.tri.tie { background: var(--text-3); opacity: 0.55; }
.tri.bare { background: var(--warn); opacity: 0.65; }
.tri-legend { font-size: 11.5px; color: var(--text-2); white-space: nowrap; }
.sw { display: inline-block; width: 8px; height: 8px; border-radius: 2px; margin: 0 4px 0 8px; }
.sw-rag { background: var(--blue); }
.sw-tie { background: var(--text-3); opacity: 0.55; }
.sw-bare { background: var(--warn); opacity: 0.65; }

/* 二元分分布 */
.dist-wrap { margin-top: 16px; }
.dist-bar { display: flex; height: 12px; border-radius: 3px; overflow: hidden; margin-top: 6px; }
.seg-ok { background: var(--success); }
.seg-half { background: var(--warn); opacity: 0.7; }
.seg-bad { background: var(--danger); opacity: 0.8; }
.dist-legend { display: flex; gap: 16px; margin-top: 6px; font-size: 12px; color: var(--text-2); }
.dotseg { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 5px; }

/* tabs */
.tabs { display: flex; gap: 6px; margin-bottom: 4px; }
.tab {
  border: 1px solid var(--border); background: var(--bg); color: var(--text-2);
  font-size: 12.5px; padding: 4px 12px; border-radius: 999px; cursor: pointer;
  transition: background 150ms ease, color 150ms ease;
}
.tab:hover:not(:disabled) { background: var(--hover); color: var(--text); }
.tab.on { background: var(--blue-bg-strong); color: var(--blue); border-color: transparent; }
.tab:disabled { opacity: 0.45; cursor: not-allowed; }

/* qa 明细 */
.qa-detail { margin-top: 18px; }
.clickable { cursor: pointer; }
.q-cell { max-width: 260px; }
.reason-cell { color: var(--text-2); font-size: 12.5px; max-width: 320px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.expand-row td { background: var(--bg-alt); }
.answer-excerpt {
  white-space: pre-wrap; font-size: 12.5px; color: var(--text); line-height: 1.6;
  max-height: 220px; overflow: auto; background: var(--bg); border: 1px solid var(--divider);
  border-radius: var(--radius-sm); padding: 8px 10px; margin-top: 6px;
}

.note { margin-top: 10px; font-size: 12.5px; }
.file-badges { display: flex; flex-wrap: wrap; gap: 6px; }
</style>
