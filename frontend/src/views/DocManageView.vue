<script setup>
import { computed, onMounted, ref } from 'vue'
import PipelineNodes from '../components/PipelineNodes.vue'
import { api } from '../api.js'
import { deleteDoc, handleError, isAdmin, reindexAll, store, updateDoc, uploadDoc } from '../store.js'

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
                    v-if="!doc.deleted_at && canEdit(doc)"
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
/* 表格/按钮/chip 等公共件样式见全局 style.css */
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
  font-weight: 600;
  color: var(--text);
  margin-bottom: 5px;
}
.vis-group { display: flex; gap: 8px; }
.vis-item {
  flex: 1;
  display: flex;
  align-items: center;
  gap: 7px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 7px 9px;
  cursor: pointer;
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
  background: var(--bg);
  transition: background var(--t);
}
.vis-item:hover { background: var(--hover); }
.vis-item small { display: block; font-weight: 400; color: var(--text-2); font-size: 11px; }
.vis-item input { accent-color: var(--blue); }
.vis-item:has(input:checked) { background: var(--active); }

.upload-slot {
  width: 100%;
  border: 1px solid var(--border);
  background: var(--bg-alt);
  color: var(--text-2);
  border-radius: var(--radius);
  padding: 12px 8px;
  font-size: 12.5px;
  font-weight: 500;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  transition: background var(--t), color var(--t);
}
.upload-slot:hover { background: var(--hover); color: var(--text); }
.upload-plus { font-size: 16px; }
.hint { margin-top: 12px; display: block; }
.reindex-strip { margin-top: 14px; border-top: 1px solid var(--border); padding-top: 12px; }

.tracked { margin-bottom: 10px; }
.list-panel .panel-body { overflow-x: auto; }
.filter-chips { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }
.doc-title {
  max-width: 340px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-weight: 600;
}

/* 行操作仅在悬停时显现（本视图特有） */
.table-btn { visibility: hidden; }
.admin-table tbody tr:hover .table-btn { visibility: visible; }

.edit-row { display: flex; flex-wrap: wrap; align-items: flex-end; gap: 14px; padding: 6px 2px; }
.edit-fld { display: flex; flex-direction: column; gap: 4px; font-size: 12px; font-weight: 600; color: var(--text-2); }
.edit-fld .scope-select { width: 160px; padding: 5px 8px; font-size: 13px; }
.vis-pick { display: inline-flex; align-items: center; gap: 4px; margin-right: 10px; font-size: 13px; color: var(--text); cursor: pointer; }
.vis-pick input { accent-color: var(--blue); }
.edit-hint { margin: 2px 2px 4px; }

@media (max-width: 860px) {
  .doc-layout { grid-template-columns: minmax(0, 1fr); }
}
</style>
