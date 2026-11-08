<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  checkReady,
  handleError,
  isAdmin,
  loadMoreConversations,
  logout,
  newTask,
  refreshConversations,
  refreshDocs,
  removeConversation,
  store,
} from '../store.js'

const route = useRoute()
const router = useRouter()
const pageTitle = computed(() => route.meta.title || '')

const NAV = [
  { to: '/docs', label: '文档管理' },
  { to: '/org-tags', label: '组织标签', admin: true },
  { to: '/users', label: '用户管理', admin: true },
  { to: '/settings', label: '系统设置' },
  { to: '/console', label: '任务' },
]

const activeConversationId = computed(() => Number(route.query.id) || null)
const showSearch = ref(false)
const keyword = ref('')
const filteredConversations = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  if (!kw) return store.conversations
  return store.conversations.filter((c) => c.title.toLowerCase().includes(kw))
})

function openConversation(id) {
  router.push({ path: '/console', query: { id } })
}
function createTask() {
  newTask()
  router.push({ path: '/console' })
}
function onKeydown(event) {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'n') {
    event.preventDefault()
    createTask()
  }
}

/* 删除任务：两击确认（与知识库删除交互一致） */
const confirmDeleteId = ref(null)
let confirmTimer = null
function requestDelete(conv) {
  if (confirmDeleteId.value === conv.conversation_id) {
    clearTimeout(confirmTimer)
    confirmDeleteId.value = null
    removeConversation(conv.conversation_id).catch(handleError)
    if (activeConversationId.value === conv.conversation_id) createTask()
    return
  }
  confirmDeleteId.value = conv.conversation_id
  clearTimeout(confirmTimer)
  confirmTimer = setTimeout(() => (confirmDeleteId.value = null), 3000)
}

function relTime(value) {
  if (!value) return ''
  const diff = (Date.now() - new Date(value).getTime()) / 1000
  if (diff < 60) return '刚刚'
  if (diff < 3600) return `${Math.floor(diff / 60)}分钟`
  if (diff < 86400) return `${Math.floor(diff / 3600)}小时`
  if (diff < 86400 * 7) return `${Math.floor(diff / 86400)}天`
  if (diff < 86400 * 30) return `${Math.floor(diff / (86400 * 7))}周`
  return `${Math.floor(diff / (86400 * 30))}月`
}

onMounted(() => {
  checkReady()
  refreshDocs().catch(handleError)
  refreshConversations().catch(handleError)
  window.addEventListener('keydown', onKeydown)
})
onUnmounted(() => {
  window.removeEventListener('keydown', onKeydown)
  clearTimeout(confirmTimer)
})

</script>

<template>
  <div class="shell">
    <!-- 左侧边栏 -->
    <aside class="sidebar">
      <RouterLink to="/console" class="side-brand h-serif">检索台</RouterLink>
      <p class="side-sub eyebrow">管理后台</p>

      <button class="btn new-task" @click="createTask">
        <span class="new-plus">＋</span> 新建任务
        <span class="kbd-hint">Ctrl+N</span>
      </button>

      <nav class="side-nav">
        <RouterLink
          v-for="item in NAV.filter((n) => !n.admin || isAdmin)"
          :key="item.to"
          :to="item.to"
          class="nav-item"
          :class="{ active: route.path === item.to }"
        >
          <span class="nav-marker"></span>
          {{ item.label }}
        </RouterLink>
      </nav>

      <!-- 任务列表 -->
      <div class="task-head">
        <span class="eyebrow">任务列表</span>
        <button
          class="search-toggle"
          :class="{ on: showSearch }"
          :title="showSearch ? '关闭搜索' : '搜索任务'"
          @click="((showSearch = !showSearch), (keyword = ''))"
        >
          <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true">
            <circle cx="11" cy="11" r="7" />
            <path d="m20 20-3.8-3.8" />
          </svg>
        </button>
      </div>
      <input
        v-if="showSearch"
        v-model="keyword"
        type="text"
        class="task-search"
        placeholder="搜索任务"
        aria-label="搜索任务"
      />
      <div class="task-scroll">
        <ul class="task-list">
          <li
            v-for="conv in filteredConversations"
            :key="conv.conversation_id"
            class="task-item"
            :class="{ active: conv.conversation_id === activeConversationId }"
            @click="openConversation(conv.conversation_id)"
          >
            <span class="task-title">{{ conv.title }}</span>
            <span class="task-time">{{ relTime(conv.updated_at) }}</span>
            <button
              class="task-del"
              :class="{ armed: confirmDeleteId === conv.conversation_id }"
              :title="confirmDeleteId === conv.conversation_id ? '再次点击确认删除' : '删除任务'"
              @click.stop="requestDelete(conv)"
            >
              {{ confirmDeleteId === conv.conversation_id ? '确认?' : '×' }}
            </button>
          </li>
        </ul>
        <p v-if="!filteredConversations.length" class="empty task-empty">
          {{ store.conversations.length ? '没有匹配的任务' : '还没有任务——提问后自动出现在这里' }}
        </p>
        <button
          v-if="store.conversationsLoaded < store.conversationsTotal"
          class="more-btn"
          @click="loadMoreConversations().catch(handleError)"
        >
          显示更多
        </button>
      </div>

      <div class="side-user" v-if="store.currentUser">
        <span class="user-tag">{{ store.currentUser.display_name }}</span>
        <span class="side-username">@{{ store.currentUser.username }}</span>
        <span v-if="isAdmin" class="badge badge-warn">管理员</span>
      </div>
      <button class="logout-btn" @click="logout">退出登录</button>
    </aside>

    <!-- 右侧：顶栏 + 内容区 -->
    <div class="shell-main">
      <header class="topbar">
        <span class="h-serif page-title">{{ pageTitle }}</span>
        <div class="health">
          <span class="dot" :class="store.readyz ? 'dot-ok' : 'dot-bad'"></span>
          <span>{{ store.readyz ? '服务就绪' : '服务不可达' }}</span>
        </div>
      </header>

      <main class="admin-main">
        <div v-if="store.notice" class="notice" role="status">{{ store.notice }}</div>
        <RouterView />
      </main>
    </div>
  </div>
</template>

<style scoped>
.shell {
  display: grid;
  grid-template-columns: 212px minmax(0, 1fr);
  min-height: 100vh;
}

/* 侧边栏：米色 + 细右缘 */
.sidebar {
  border-right: 1px solid var(--border);
  background: var(--bg-alt);
  padding: 16px 12px 12px;
  display: flex;
  flex-direction: column;
  gap: 5px;
  position: sticky;
  top: 0;
  height: 100vh;
  overflow: hidden;
}
.side-brand {
  font-size: 18px;
  font-weight: 600;
  color: var(--text);
  text-decoration: none;
  padding-left: 6px;
  transition: color var(--t);
}
.side-brand:hover { color: var(--blue); }
.side-sub { padding-left: 7px; margin: 2px 0 10px; }

/* 新建任务 */
.new-task {
  display: flex;
  align-items: center;
  gap: 7px;
  width: 100%;
  font-size: 13px;
  padding: 7px 12px;
  margin-bottom: 6px;
}
.new-plus { font-size: 15px; line-height: 1; }
.kbd-hint {
  margin-left: auto;
  font-size: 10px;
  color: var(--text-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 0 5px;
  background: var(--bg);
}

.side-nav { display: flex; flex-direction: column; gap: 2px; margin-bottom: 6px; }
.nav-item {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13.5px;
  color: var(--text-2);
  text-decoration: none;
  padding: 6px 10px;
  border-radius: var(--radius);
  transition: background var(--t), color var(--t);
}
.nav-item:hover { background: var(--hover); color: var(--text); }
/* 选中：仅底色加深，无描边无阴影跳变 */
.nav-item.active {
  background: var(--active);
  color: var(--text);
  font-weight: 600;
}
.nav-marker {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: transparent;
  flex-shrink: 0;
  transition: background var(--t);
}
.nav-item:hover .nav-marker { background: var(--text-3); }
.nav-item.active .nav-marker { background: var(--blue); }

/* 任务列表 */
.task-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 4px 6px;
  border-top: 1px solid var(--border);
  margin-top: 2px;
}
.search-toggle {
  border: none;
  background: transparent;
  color: var(--text-3);
  cursor: pointer;
  display: flex;
  padding: 2px;
  border-radius: var(--radius-sm);
  transition: background var(--t), color var(--t);
}
.search-toggle:hover { background: var(--hover); color: var(--text); }
.search-toggle.on { color: var(--blue); }
.task-search {
  margin-bottom: 6px;
  padding: 4px 2px;
  font-size: 12.5px;
}
.task-scroll {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
}
.task-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 1px; }
.task-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 8px 6px 20px; /* 左侧预留拖拽手柄位 */
  border-radius: var(--radius);
  cursor: pointer;
  position: relative;
  transition: background var(--t);
}
/* Drag Handle Illusion：悬停显隐 ⋮⋮，仅透明度变化 */
.task-item::before {
  content: '⋮⋮';
  position: absolute;
  left: 5px;
  top: 6px;
  font-size: 12px;
  line-height: 1;
  letter-spacing: -3px;
  color: var(--text-3);
  opacity: 0;
  transition: opacity var(--t);
}
.task-item:hover::before { opacity: 1; }
.task-item:hover { background: var(--hover); }
.task-item.active {
  background: var(--active);
}
.task-title {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12.5px;
  color: var(--text);
}
.task-item.active .task-title { font-weight: 600; }
.task-time { font-size: 10.5px; color: var(--text-3); flex-shrink: 0; }
.task-del {
  border: none;
  background: transparent;
  color: var(--text-3);
  font-weight: 500;
  font-size: 12px;
  padding: 1px 5px;
  border-radius: var(--radius-sm);
  opacity: 0;
  flex-shrink: 0;
  cursor: pointer;
  transition: opacity var(--t), background var(--t), color var(--t);
}
.task-item:hover .task-del { opacity: 1; }
.task-del:hover { background: var(--danger-bg); color: var(--danger-strong); }
.task-del.armed { opacity: 1; color: var(--danger-strong); }
.task-empty { padding: 14px 0; font-size: 12px; }
.more-btn {
  border: none;
  background: transparent;
  color: var(--text-2);
  font-weight: 500;
  font-size: 12px;
  cursor: pointer;
  padding: 6px 8px;
  border-radius: var(--radius);
  text-align: left;
  transition: background var(--t), color var(--t);
}
.more-btn:hover { background: var(--hover); color: var(--text); }

.side-user {
  margin-top: 8px;
  display: flex;
  flex-wrap: wrap;
  gap: 4px 6px;
  align-items: center;
  padding: 10px 6px 0;
  border-top: 1px solid var(--border);
}
.side-username { font-size: 11px; color: var(--text-3); }
.logout-btn {
  margin-top: 6px;
  align-self: flex-start;
  border: none;
  background: transparent;
  color: var(--text-2);
  font-weight: 500;
  font-size: 12px;
  cursor: pointer;
  padding: 4px 8px;
  border-radius: var(--radius);
  transition: background var(--t), color var(--t);
}
.logout-btn:hover { background: var(--hover); color: var(--text); }

/* 顶栏 */
.topbar {
  height: 52px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  padding: 0 18px;
  border-bottom: 1px solid var(--border);
  background: var(--bg);
  position: sticky;
  top: 0;
  z-index: 5;
}
.page-title { font-size: 17px; }
.health { font-size: 12px; color: var(--text-2); display: flex; align-items: center; gap: 7px; }

/* 内容区：随窗口铺满，不限宽（宽屏留白由引用栏列吸收） */
.admin-main {
  padding: 16px 24px 24px;
}
.notice {
  font-size: 12.5px;
  color: var(--text);
  border: 1px solid rgba(154, 110, 0, 0.25);
  background: var(--warn-bg);
  padding: 7px 12px;
  margin-bottom: 10px;
  border-radius: var(--radius);
}

@media (max-width: 860px) {
  .shell { grid-template-columns: 1fr; }
  .sidebar { position: static; height: auto; }
}
</style>
