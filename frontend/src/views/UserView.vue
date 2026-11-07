<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'
import { handleError, store } from '../store.js'

/* 用户管理：授予/撤销管理员 + 给用户打组织标签（最后一管理员保护由后端守卫） */
const users = ref([])
const tags = ref([])
const loading = ref(false)
const editingId = ref(null)
const draftTags = ref([])

async function refresh() {
  loading.value = true
  try {
    const [userList, tagList] = await Promise.all([api.listUsers(), api.listOrgTags()])
    users.value = userList.items
    tags.value = tagList.items
  } catch (err) {
    handleError(err)
  } finally {
    loading.value = false
  }
}
onMounted(refresh)

async function toggleAdmin(user) {
  try {
    const updated = await api.updateUserAdmin(user.user_id, !user.is_admin)
    Object.assign(user, updated)
    store.notice = updated.is_admin ? '已授予管理员。' : '已撤销管理员。'
  } catch (err) {
    store.notice = err.message
  }
}

function startEdit(user) {
  editingId.value = user.user_id
  draftTags.value = [...user.org_tag_ids]
}
function toggleDraft(tagId) {
  const idx = draftTags.value.indexOf(tagId)
  if (idx >= 0) draftTags.value.splice(idx, 1)
  else draftTags.value.push(tagId)
}
async function submitTags(user) {
  editingId.value = null
  try {
    const updated = await api.updateUserTags(user.user_id, draftTags.value)
    Object.assign(user, updated)
    store.notice = '标签已更新。'
  } catch (err) {
    store.notice = err.message
  }
}

function fmtTime(value) {
  if (!value) return '—'
  return String(value).slice(0, 10)
}
</script>

<template>
  <section class="panel user-panel">
    <div class="panel-title">
      <span class="h-serif">用户管理</span>
      <span class="eyebrow">{{ users.length }} 名用户 · 打标决定可见范围</span>
    </div>
    <div class="panel-body">
      <table class="admin-table">
        <thead>
          <tr>
            <th>用户</th>
            <th>角色</th>
            <th>组织标签</th>
            <th>注册于</th>
            <th class="ops"></th>
          </tr>
        </thead>
        <tbody>
          <template v-for="user in users" :key="user.user_id">
            <tr>
              <td>
                <span class="user-name">{{ user.display_name }}</span>
                <span class="muted">@{{ user.username }}</span>
              </td>
              <td>
                <span v-if="user.is_admin" class="badge badge-warn">管理员</span>
                <span v-else class="badge">成员</span>
              </td>
              <td>
                <div class="tag-chips">
                  <span v-for="name in user.org_tags" :key="name" class="chip">{{ name }}</span>
                  <span v-if="!user.org_tags.length" class="muted">—</span>
                </div>
              </td>
              <td class="muted">{{ fmtTime(user.created_at) }}</td>
              <td class="ops">
                <button class="table-btn" @click="startEdit(user)">编辑标签</button>
                <button class="table-btn danger" @click="toggleAdmin(user)">
                  {{ user.is_admin ? '撤销管理员' : '设为管理员' }}
                </button>
              </td>
            </tr>
            <!-- 展开的标签编辑行 -->
            <tr v-if="editingId === user.user_id">
              <td colspan="5" class="edit-cell">
                <div class="tag-picker">
                  <label v-for="tag in tags" :key="tag.tag_id" class="pick-item">
                    <input
                      type="checkbox"
                      :checked="draftTags.includes(tag.tag_id)"
                      @change="toggleDraft(tag.tag_id)"
                    />
                    <span>{{ tag.name }}</span>
                  </label>
                  <p v-if="!tags.length" class="empty">暂无标签——先到「组织标签」创建</p>
                </div>
                <div class="edit-ops">
                  <button class="btn mini" @click="submitTags(user)">保存标签</button>
                  <button class="btn btn-ghost mini" @click="editingId = null">取消</button>
                </div>
              </td>
            </tr>
          </template>
          <tr v-if="!users.length && !loading">
            <td colspan="5"><p class="empty">没有用户</p></td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>

<style scoped>
.user-panel { max-width: 960px; }

.admin-table { width: 100%; border-collapse: collapse; font-size: 13.5px; }
.admin-table th {
  text-align: left;
  font-size: 11px;
  letter-spacing: 0.1em;
  color: var(--muted);
  border-bottom: 2px dashed var(--ink);
  padding: 4px 8px;
}
.admin-table td { padding: 7px 8px; border-bottom: 1.5px dashed rgba(44, 44, 44, 0.25); vertical-align: top; }
.admin-table tbody tr:hover td { background: rgba(255, 217, 51, 0.14); }
.admin-table .ops { text-align: right; white-space: nowrap; }
.muted { color: var(--muted); font-size: 12px; }
.user-name { font-weight: 700; margin-right: 6px; }

.tag-chips { display: flex; flex-wrap: wrap; gap: 4px; max-width: 320px; }
.chip {
  border: 1.5px dashed var(--ink);
  border-radius: var(--radius);
  background: rgba(78, 205, 196, 0.22);
  font-size: 11.5px;
  padding: 0 7px;
  transform: rotate(-0.6deg);
}
.chip:nth-child(2n) { transform: rotate(0.6deg); }

.edit-cell { background: var(--paper-alt); }
.tag-picker { display: flex; flex-wrap: wrap; gap: 10px; padding: 6px 2px; }
.pick-item { display: flex; align-items: center; gap: 5px; font-size: 13px; cursor: pointer; }
.pick-item input { accent-color: var(--red); }
.edit-ops { display: flex; gap: 8px; padding-bottom: 6px; }

.table-btn {
  border: none;
  background: transparent;
  color: var(--muted);
  font-family: var(--hand);
  font-weight: 700;
  font-size: 12px;
  cursor: pointer;
  padding: 2px 6px;
}
.table-btn:hover { color: var(--ink); text-decoration: underline wavy var(--teal) 1px; text-underline-offset: 3px; }
.table-btn.danger:hover { color: var(--red); text-decoration-color: var(--red); }
.mini { padding: 4px 10px; font-size: 12px; }
</style>
