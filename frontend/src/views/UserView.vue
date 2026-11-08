<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../api.js'
import { handleError, store } from '../store.js'

/* 多条件查询、账号删除、管理员授权与组织标签管理。 */
const router = useRouter()
const PAGE_SIZE = 20
const users = ref([])
const tags = ref([])
const loading = ref(false)
const total = ref(0)
const page = ref(1)
const pageCount = computed(() => Math.max(1, Math.ceil(total.value / PAGE_SIZE)))
const keyword = ref('')
const role = ref('')
const selectedTagIds = ref([])
const appliedFilters = ref({})
const editingId = ref(null)
const draftTags = ref([])
const confirmDeleteId = ref(null)
const busyUserId = ref(null)
let loadController = null

async function refresh() {
  loadController?.abort()
  const controller = new AbortController()
  loadController = controller
  loading.value = true
  try {
    const userList = await api.listUsers(
      PAGE_SIZE, (page.value - 1) * PAGE_SIZE, appliedFilters.value, controller.signal
    )
    if (controller.signal.aborted) return
    total.value = userList.total
    if (page.value > pageCount.value) {
      page.value = pageCount.value
      return refresh()
    }
    users.value = userList.items
    editingId.value = null
    confirmDeleteId.value = null
  } catch (err) {
    if (!controller.signal.aborted) handleError(err)
  } finally {
    if (loadController === controller) {
      loadController = null
      loading.value = false
    }
  }
}
onMounted(() => {
  refresh()
  api.listOrgTags().then((data) => { tags.value = data.items }).catch(handleError)
})
onUnmounted(() => loadController?.abort())

function searchUsers() {
  store.notice = ''
  appliedFilters.value = {
    keyword: keyword.value.trim(),
    isAdmin: role.value === '' ? null : role.value === 'admin',
    orgTagIds: [...selectedTagIds.value],
  }
  page.value = 1
  refresh()
}

function resetFilters() {
  keyword.value = ''
  role.value = ''
  selectedTagIds.value = []
  searchUsers()
}

function changePage(step) {
  page.value += step
  refresh()
}

async function toggleAdmin(user) {
  if (busyUserId.value !== null) return
  busyUserId.value = user.user_id
  try {
    const updated = await api.updateUserAdmin(user.user_id, !user.is_admin)
    store.notice = updated.is_admin ? '已授予管理员。' : '已撤销管理员。'
    if (user.user_id === store.currentUser?.user_id) {
      store.currentUser.is_admin = updated.is_admin
      if (!updated.is_admin) {
        router.replace('/console')
        return
      }
    }
    await refresh()
  } catch (err) {
    handleError(err)
  } finally {
    busyUserId.value = null
  }
}

function startEdit(user) {
  confirmDeleteId.value = null
  editingId.value = user.user_id
  draftTags.value = [...user.org_tag_ids]
}
function toggleDraft(tagId) {
  const idx = draftTags.value.indexOf(tagId)
  if (idx >= 0) draftTags.value.splice(idx, 1)
  else draftTags.value.push(tagId)
}
async function submitTags(user) {
  if (busyUserId.value !== null) return
  busyUserId.value = user.user_id
  try {
    await api.updateUserTags(user.user_id, draftTags.value)
    store.notice = '标签已更新。'
    await refresh()
  } catch (err) {
    handleError(err)
  } finally {
    busyUserId.value = null
  }
}

function requestDelete(user) {
  editingId.value = null
  confirmDeleteId.value = user.user_id
}

async function deleteUser(user) {
  if (busyUserId.value !== null) return
  busyUserId.value = user.user_id
  try {
    await api.deleteUser(user.user_id)
    store.notice = `已删除用户 @${user.username}。`
    await refresh()
  } catch (err) {
    handleError(err)
  } finally {
    busyUserId.value = null
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
      <span class="eyebrow">共 {{ total }} 名用户</span>
    </div>
    <div class="panel-body">
      <form class="user-filters" @submit.prevent="searchUsers">
        <div class="filter-row">
          <label class="keyword-filter">
            <span class="filter-label">用户</span>
            <input v-model="keyword" type="text" maxlength="100" placeholder="用户名或显示名" />
          </label>
          <label class="role-filter">
            <span id="user-role-label" class="filter-label">角色</span>
            <select v-model="role" class="scope-select" aria-labelledby="user-role-label">
              <option value="">全部角色</option>
              <option value="admin">管理员</option>
              <option value="member">成员</option>
            </select>
          </label>
          <button class="btn" type="submit" :disabled="loading || busyUserId !== null">查询</button>
          <button class="btn btn-ghost" type="button" :disabled="busyUserId !== null" @click="resetFilters">重置</button>
        </div>
        <fieldset class="tag-filter">
          <legend class="filter-label">组织标签</legend>
          <div class="filter-tags">
            <label v-for="tag in tags" :key="tag.tag_id" class="filter-tag">
              <input v-model="selectedTagIds" type="checkbox" :value="tag.tag_id" />
              <span>{{ tag.name }}</span>
            </label>
            <span v-if="!tags.length" class="muted">暂无组织标签</span>
          </div>
          <p class="filter-hint">支持多选，匹配任意一个所选标签即可；未选择时查询全部组织。</p>
        </fieldset>
      </form>
      <p v-if="loading" class="loading-note" role="status">正在查询用户…</p>
      <div class="table-scroll" :aria-busy="loading" role="region" aria-label="用户列表" tabindex="0">
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
                  <span v-for="name in user.org_tags" :key="name" class="chip static">{{ name }}</span>
                  <span v-if="!user.org_tags.length" class="muted">—</span>
                </div>
              </td>
              <td class="muted">{{ fmtTime(user.created_at) }}</td>
              <td class="ops">
                <button class="table-btn" :disabled="loading || busyUserId !== null" @click="startEdit(user)">编辑标签</button>
                <button class="table-btn" :disabled="loading || busyUserId !== null" @click="toggleAdmin(user)">
                  {{ user.is_admin ? '撤销管理员' : '设为管理员' }}
                </button>
                <button
                  class="table-btn danger"
                  :disabled="loading || busyUserId !== null || user.user_id === store.currentUser?.user_id"
                  :title="user.user_id === store.currentUser?.user_id ? '不能删除当前登录账号' : '删除用户'"
                  @click="requestDelete(user)"
                >删除用户</button>
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
                  <button class="btn mini" :disabled="busyUserId !== null" @click="submitTags(user)">保存标签</button>
                  <button class="btn btn-ghost mini" :disabled="busyUserId !== null" @click="editingId = null">取消</button>
                </div>
              </td>
            </tr>
            <tr v-if="confirmDeleteId === user.user_id">
              <td colspan="5" class="edit-cell">
                <div class="delete-confirm" role="alert">
                  <p>确认删除 @{{ user.username }}？账号及成员关系将删除，已上传文档和问答审计记录保留。</p>
                  <div class="edit-ops">
                    <button class="btn btn-danger mini" :disabled="loading || busyUserId !== null" @click="deleteUser(user)">确认删除</button>
                    <button class="btn btn-ghost mini" :disabled="busyUserId !== null" @click="confirmDeleteId = null">取消</button>
                  </div>
                </div>
              </td>
            </tr>
          </template>
          <tr v-if="!users.length && !loading">
            <td colspan="5"><p class="empty">没有符合条件的用户，可调整查询条件。</p></td>
          </tr>
        </tbody>
      </table>
      </div>
      <div class="pagination">
        <span class="muted">共 {{ total }} 名用户 · 第 {{ page }} / {{ pageCount }} 页</span>
        <div class="page-buttons">
          <button class="btn btn-ghost mini" :disabled="loading || busyUserId !== null || page <= 1" @click="changePage(-1)">上一页</button>
          <button class="btn btn-ghost mini" :disabled="loading || busyUserId !== null || page >= pageCount" @click="changePage(1)">下一页</button>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
/* 表格/chip/按钮公共件样式见全局 style.css */
.user-panel { max-width: 1120px; min-width: 0; }
.user-filters { display: grid; gap: 16px; margin-bottom: 20px; }
.filter-row { display: flex; align-items: end; flex-wrap: wrap; gap: 12px; }
.keyword-filter { flex: 1; min-width: 180px; }
.role-filter { width: 160px; }
.filter-label { display: block; margin-bottom: 6px; font-size: 12.5px; font-weight: 600; color: var(--text-2); }
.tag-filter { border: 0; padding: 0; margin: 0; min-width: 0; }
.filter-tags { display: flex; flex-wrap: wrap; gap: 8px 14px; }
.filter-tag { display: inline-flex; align-items: center; gap: 5px; cursor: pointer; font-size: 13px; }
.filter-tag input { accent-color: var(--blue); }
.filter-hint { margin: 8px 0 0; font-size: 12px; color: var(--text-2); }
.loading-note { font-size: 12.5px; color: var(--text-2); }
.table-scroll { overflow-x: auto; }
.admin-table { min-width: 720px; }
.admin-table td { vertical-align: top; }
.admin-table .ops { white-space: normal; min-width: 220px; }
.table-btn:disabled { opacity: 0.45; cursor: not-allowed; }
.delete-confirm p { margin: 4px 0 10px; color: var(--danger-strong); }
.pagination { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; padding-top: 16px; font-size: 12px; }
.page-buttons { display: flex; gap: 8px; }
.user-name { font-weight: 600; margin-right: 6px; }

.tag-chips { display: flex; flex-wrap: wrap; gap: 4px; max-width: 320px; }
.tag-chips .chip { font-size: 11.5px; padding: 0 7px; }

.tag-picker { display: flex; flex-wrap: wrap; gap: 10px; padding: 6px 2px; }
.pick-item { display: flex; align-items: center; gap: 5px; font-size: 13px; cursor: pointer; color: var(--text); }
.pick-item input { accent-color: var(--blue); }
.edit-ops { padding-bottom: 6px; }
</style>
