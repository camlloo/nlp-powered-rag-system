<script setup>
import { computed, onMounted, ref } from 'vue'
import PipelineNodes from '../components/PipelineNodes.vue'
import { api } from '../api.js'
import { deleteDoc, handleError, isAdmin, reindexAll, store, updateDoc } from '../store.js'

/* 文档管理：归属个人/某组织 + 公开/私有；清单只含当前用户可见的文档；
   上传者本人与管理员可修改归属/可见性（即时影响检索范围） */
const docs = computed(() => store.docs)
const myOrgNames = ref([])
const scope = ref('personal')
const visibility = ref('private')
const filter = ref('all')

onMounted(async () => {
  if (isAdmin.value) {
    try {
      myOrgNames.value = (await api.listOrgTags()).items.map((t) => t.name)
    } catch (err) {
      handleError(err)
    }
  } else {
    myOrgNames.value = [...(store.currentUser?.org_tags || [])]
  }
})

const filteredDocs = computed(() => {
  if (filter.value === 'all') return docs.value
  if (filter.value === 'personal') return docs.value.filter((d) => !d.org_tag_name)
  return docs.value.filter((d) => d.org_tag_name === filter.value)
})

const fileInput = ref(null)
function pickFile() {
  fileInput.value?.click()
}
function onFileChange(event) {
  const file = event.target.files?.[0]
  if (file) uploadDoc(file, scope.value, visibility.value).catch(handleError)
  event.target.value = ''
}

function removeDoc(doc) {
  deleteDoc(doc).catch(handleError)
}

/* 行内编辑归属/可见性 */
const editingId = ref(null)
const editScope = ref('personal')
const editVisibility = ref('private')

function canEdit(doc) {
  return isAdmin.value || doc.uploader_user_id === store.currentUser?.user_id
}
function startEdit(doc) {
  editingId.value = doc.document_id
  editScope.value = doc.org_tag_name || 'personal'
  editVisibility.value = doc.visibility
}
async function saveEdit(doc) {
  editingId.value = null
  await updateDoc(doc, editScope.value, editVisibility.value).catch(handleError)
}

const STATUS_BADGE = {
  succeeded: 'badge-ok',
  needs_review: 'badge-warn',
  failed: 'badge-bad',
  pending: 'badge-run',
}
const STATUS_TEXT = {
  succeeded: '已索引',
  needs_review: '待复核',
  failed: '失败',
  pending: '解析中',
}
</script>

<template>
  <div class="doc-layout">
    <section class="panel side-panel">
      <div class="panel-title">
        <span class="h-serif">上传文档</span>
        <span class="eyebrow">TXT · MD · DOCX · PDF</span>
      </div>
      <div class="panel-body">
        <label class="fld">
          <span class="fld-label">归属</span>
          <select v-model="scope" class="scope-select">
            <option value="personal">个人</option>
            <option v-for="name in myOrgNames" :key="name" :value="name">{{ name }}</option>
          </select>
        </label>
        <div class="fld">
          <span class="fld-label">可见性</span>
          <div class="vis-group">
            <label class="vis-item">
              <input v-model="visibility" type="radio" value="private" />
              <span>私有<small>仅自己{{ scope !== 'personal' ? '与该组织' : '' }}</small></span>
            </label>
            <label class="vis-item">
              <input v-model="visibility" type="radio" value="public" />
              <span>公开<small>所有人可检索</small></span>
            </label>
          </div>
        </div>
        <button type="button" class="upload-slot" @click="pickFile">
          <span class="upload-plus">＋</span>
          选择文件上传
        </button>
        <input
          ref="fileInput"
          type="file"
          accept=".txt,.md,.markdown,.docx,.pdf"
          style="display: none"
          @change="onFileChange"
        />
        <p class="hint eyebrow">上传后自动解析→切块→向量化→发布</p>

        <div v-if="isAdmin" class="reindex-strip">
          <button type="button" class="btn btn-ghost mini" @click="reindexAll().catch(handleError)">
            全量重建索引
          </button>
        </div>
      </div>
    </section>

    <section class="panel list-panel">
      <div class="panel-title">
        <span class="h-serif">文档</span>
        <span class="eyebrow">{{ filteredDocs.length }} 份可见</span>
      </div>
      <div class="panel-body">
        <div class="tracked">
          <PipelineNodes v-for="t in store.trackedJobs" :key="t.jobId" :job="t" />
        </div>
        <div class="filter-chips">
          <button
            class="chip"
            :class="{ active: filter === 'all' }"
            @click="filter = 'all'"
          >全部</button>
          <button
            class="chip"
            :class="{ active: filter === 'personal' }"
            @click="filter = 'personal'"
          >个人</button>
          <button
            v-for="name in myOrgNames"
            :key="name"
            class="chip"
            :class="{ active: filter === name }"
            @click="filter = name"
          >{{ name }}</button>
        </div>
        <table class="admin-table">
          <thead>
            <tr>
              <th>标题</th>
              <th>归属</th>
              <th>可见性</th>
              <th>状态</th>
              <th class="ops"></th>
            </tr>
          </thead>
          <tbody>
            <template v-for="doc in filteredDocs" :key="doc.document_id">
              <tr :class="{ purged: doc.deleted_at }">
                <td class="doc-title" :title="doc.title">{{ doc.title }}</td>
                <td>
                  <span v-if="doc.org_tag_name" class="chip static">{{ doc.org_tag_name }}</span>
                  <span v-else class="muted">个人</span>
                </td>
                <td>
                  <span class="badge" :class="doc.visibility === 'public' ? 'badge-warn' : ''">
                    {{ doc.visibility === 'public' ? '公开' : '私有' }}
                  </span>
                </td>
                <td>
                  <span class="badge" :class="STATUS_BADGE[doc.parse_status] || ''">
                    {{ doc.deleted_at ? '已删除' : STATUS_TEXT[doc.parse_status] }}
                  </span>
                </td>
                <td class="ops">
                  <button
                    v-if="!doc.deleted_at && canEdit(doc)"
                    type="button"
                    class="table-btn"
                    title="修改归属与可见性"
                    @click="startEdit(doc)"
                  >
                    编辑
                  </button>
                  <button
                    v-if="!doc.deleted_at"
                    type="button"
                    class="table-btn danger"
                    title="删除文档"
                    @click="removeDoc(doc)"
                  >
                    ×
                  </button>
                </td>
              </tr>
              <!-- 展开的属性编辑行 -->
              <tr v-if="editingId === doc.document_id">
                <td colspan="5" class="edit-cell">
                  <div class="edit-row">
                    <label class="edit-fld">
                      <span>归属</span>
                      <select v-model="editScope" class="scope-select">
                        <option value="personal">个人</option>
                        <option v-for="name in myOrgNames" :key="name" :value="name">{{ name }}</option>
                      </select>
                    </label>
                    <div class="edit-fld">
                      <span>可见性</span>
                      <label class="vis-pick">
                        <input v-model="editVisibility" type="radio" value="private" /> 私有
                      </label>
                      <label class="vis-pick">
                        <input v-model="editVisibility" type="radio" value="public" /> 公开
                      </label>
                    </div>
                    <div class="edit-ops">
                      <button class="btn mini" @click="saveEdit(doc)">保存</button>
                      <button class="btn btn-ghost mini" @click="editingId = null">取消</button>
                    </div>
                  </div>
                  <p class="edit-hint eyebrow">修改即时影响检索可见范围</p>
                </td>
              </tr>
            </template>
            <tr v-if="!filteredDocs.length">
              <td colspan="5"><p class="empty">还没有可见的文档——在左侧上传第一个文件</p></td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </div>
</template>

<style scoped>
.doc-layout {
  display: grid;
  grid-template-columns: 280px minmax(0, 1fr);
  gap: 16px;
  align-items: start;
}

.fld { margin-bottom: 14px; }
.fld-label {
  display: block;
  font-size: 13px;
  font-weight: 700;
  color: var(--ink);
  margin-bottom: 5px;
}
.scope-select {
  width: 100%;
  border: 2px dashed var(--ink);
  border-radius: var(--radius);
  background: var(--paper);
  color: var(--ink);
  padding: 8px 10px;
  font-family: var(--sans);
  font-size: 14px;
}
.vis-group { display: flex; gap: 8px; }
.vis-item {
  flex: 1;
  display: flex;
  align-items: center;
  gap: 7px;
  border: 2px dashed var(--ink);
  border-radius: var(--radius);
  padding: 7px 9px;
  cursor: pointer;
  font-size: 13px;
  font-weight: 700;
  color: var(--ink);
}
.vis-item small { display: block; font-weight: 400; color: var(--muted); font-size: 11px; }
.vis-item input { accent-color: var(--red); }
.vis-item:has(input:checked) { background: rgba(255, 217, 51, 0.35); border-color: var(--ink); }

.upload-slot {
  width: 100%;
  border: 2px dashed var(--ink);
  background: rgba(255, 217, 51, 0.18);
  color: var(--muted);
  border-radius: var(--radius);
  padding: 12px 8px;
  font-size: 12.5px;
  font-weight: 700;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  transform: rotate(-0.3deg);
  transition: all 0.2s ease-in-out;
}
.upload-slot:hover {
  border-color: var(--red);
  color: var(--ink);
  transform: translate(1px, 1px) rotate(0.3deg);
  box-shadow: 3px 3px 0px var(--teal);
}
.upload-plus { font-size: 16px; }
.hint { margin-top: 12px; }
.reindex-strip { margin-top: 14px; border-top: 1.5px dashed rgba(44, 44, 44, 0.3); padding-top: 12px; }

.tracked { margin-bottom: 10px; }
.filter-chips { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }
.chip {
  border: 1.5px dashed var(--ink);
  border-radius: var(--radius);
  background: transparent;
  color: var(--muted);
  font-size: 12px;
  font-weight: 700;
  padding: 2px 10px;
  cursor: pointer;
  transform: rotate(-0.5deg);
}
.chip:hover { color: var(--ink); background: rgba(255, 217, 51, 0.25); }
.chip.active {
  background: rgba(255, 217, 51, 0.45);
  color: var(--ink);
  box-shadow: 2px 2px 0px var(--red);
}
.chip.static { cursor: default; background: rgba(78, 205, 196, 0.22); color: var(--ink); }

.admin-table { width: 100%; border-collapse: collapse; font-size: 13.5px; }
.admin-table th {
  text-align: left;
  font-size: 11px;
  letter-spacing: 0.1em;
  color: var(--muted);
  border-bottom: 2px dashed var(--ink);
  padding: 4px 8px;
}
.admin-table td { padding: 7px 8px; border-bottom: 1.5px dashed rgba(44, 44, 44, 0.25); }
.admin-table tbody tr:hover td { background: rgba(255, 217, 51, 0.14); }
.admin-table tr.purged { opacity: 0.45; }
.doc-title {
  max-width: 340px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-weight: 700;
}
.muted { color: var(--muted); font-size: 12px; }
.ops { text-align: right; width: 96px; white-space: nowrap; }

.edit-cell { background: var(--paper-alt); }
.edit-row { display: flex; flex-wrap: wrap; align-items: flex-end; gap: 14px; padding: 6px 2px; }
.edit-fld { display: flex; flex-direction: column; gap: 4px; font-size: 12px; font-weight: 700; color: var(--muted); }
.edit-fld > span { letter-spacing: 0.08em; }
.edit-fld .scope-select { width: 160px; padding: 5px 8px; font-size: 13px; }
.vis-pick { display: inline-flex; align-items: center; gap: 4px; margin-right: 10px; font-size: 13px; color: var(--ink); cursor: pointer; }
.vis-pick input { accent-color: var(--red); }
.edit-ops { display: flex; gap: 8px; }
.edit-hint { margin: 2px 2px 4px; }

.table-btn {
  border: none;
  background: transparent;
  color: var(--muted);
  font-family: var(--hand);
  font-weight: 700;
  font-size: 12px;
  cursor: pointer;
  padding: 0 4px;
  visibility: hidden;
}
.admin-table tbody tr:hover .table-btn { visibility: visible; }
.table-btn:hover { color: var(--ink); text-decoration: underline wavy var(--teal) 1px; text-underline-offset: 3px; }
.table-btn.danger:hover { color: var(--red); text-decoration-color: var(--red); }
.mini { padding: 4px 10px; font-size: 12px; }

@media (max-width: 860px) {
  .doc-layout { grid-template-columns: 1fr; }
}
</style>
