<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'
import { handleError, store } from '../store.js'

/* 组织标签管理：字典 CRUD + 使用计数；删除会剥离文档/块引用并即时影响可见性 */
const tags = ref([])
const loading = ref(false)
const newName = ref('')
const editingId = ref(null)
const editName = ref('')
const confirmId = ref(null)
let confirmTimer = null

async function refresh() {
  loading.value = true
  try {
    tags.value = (await api.listOrgTags()).items
  } catch (err) {
    handleError(err)
  } finally {
    loading.value = false
  }
}
onMounted(refresh)

async function create() {
  const name = newName.value.trim()
  if (!name) return
  try {
    await api.createOrgTag(name)
    newName.value = ''
    store.notice = '标签已创建。'
    await refresh()
  } catch (err) {
    store.notice = err.message
  }
}

function startEdit(tag) {
  editingId.value = tag.tag_id
  editName.value = tag.name
}
async function submitEdit() {
  const tagId = editingId.value
  const name = editName.value.trim()
  editingId.value = null
  if (!name) return
  try {
    await api.renameOrgTag(tagId, name)
    store.notice = '标签已重命名。'
  } catch (err) {
    store.notice = err.message
  }
  await refresh()
}

function requestDelete(tagId) {
  if (confirmId.value === tagId) {
    clearTimeout(confirmTimer)
    confirmId.value = null
    doDelete(tagId)
    return
  }
  confirmId.value = tagId
  clearTimeout(confirmTimer)
  confirmTimer = setTimeout(() => (confirmId.value = null), 3000)
}

async function doDelete(tagId) {
  try {
    const result = await api.deleteOrgTag(tagId)
    store.notice =
      result.detached_documents > 0
        ? `标签已删除：${result.detached_documents} 个文档解除关联，涉及 ${result.kbs_touched.length} 个知识库。`
        : '标签已删除。'
  } catch (err) {
    store.notice = err.message
  }
  await refresh()
}

function fmtTime(value) {
  if (!value) return '—'
  return String(value).slice(0, 10)
}
</script>

<template>
  <section class="panel tag-panel">
    <div class="panel-title">
      <span class="h-serif">组织标签</span>
      <span class="eyebrow">部门 / 团队 · 控制文档可见范围</span>
    </div>
    <div class="panel-body">
      <p class="intro">
        文档上传时继承上传者的第一个组织标签；只有<b>同标签用户、上传者本人与公开文档</b>在检索中可见。
        删除标签会解除文档与用户的关联，并立即生效。
      </p>

      <div class="row create-row">
        <input
          v-model="newName"
          type="text"
          placeholder="新标签名称，如：研发部"
          maxlength="64"
          aria-label="新标签名称"
          @keydown.enter.prevent="create"
        />
        <button class="btn" type="button" :disabled="!newName.trim()" @click="create">创建标签</button>
      </div>

      <table class="admin-table">
        <thead>
          <tr>
            <th>名称</th>
            <th class="num">用户数</th>
            <th class="num">文档数</th>
            <th>创建于</th>
            <th class="ops"></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="tag in tags" :key="tag.tag_id">
            <td>
              <template v-if="editingId === tag.tag_id">
                <input
                  v-model="editName"
                  type="text"
                  class="edit-input"
                  maxlength="64"
                  @keydown.enter.prevent="submitEdit"
                  @keydown.esc="editingId = null"
                />
              </template>
              <template v-else>{{ tag.name }}</template>
            </td>
            <td class="num">
              <span class="badge" :class="tag.user_count ? 'badge-ok' : ''">{{ tag.user_count }}</span>
            </td>
            <td class="num">
              <span class="badge" :class="tag.document_count ? 'badge-run' : ''">{{ tag.document_count }}</span>
            </td>
            <td class="muted">{{ fmtTime(tag.created_at) }}</td>
            <td class="ops">
              <template v-if="editingId === tag.tag_id">
                <button class="btn btn-ghost mini" @click="submitEdit">保存</button>
                <button class="btn btn-ghost mini" @click="editingId = null">取消</button>
              </template>
              <template v-else>
                <button class="table-btn" @click="startEdit(tag)">重命名</button>
                <button
                  class="table-btn danger"
                  :class="{ armed: confirmId === tag.tag_id }"
                  @click="requestDelete(tag.tag_id)"
                >
                  {{ confirmId === tag.tag_id ? '确认删除?' : '删除' }}
                </button>
              </template>
            </td>
          </tr>
          <tr v-if="!tags.length && !loading">
            <td colspan="5"><p class="empty">还没有组织标签——在上方创建第一个</p></td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>

<style scoped>
/* 表格/按钮公共件样式见全局 style.css */
.tag-panel { max-width: 860px; }
.intro { font-size: 12.5px; color: var(--text-2); margin: 0 0 12px; }
.create-row { display: flex; gap: 8px; margin-bottom: 14px; align-items: flex-end; }
.create-row input { flex: 1; }

.edit-input { max-width: 220px; }
</style>
