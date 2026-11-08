<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { setToken } from '../api.js'
import { authenticate, refreshMe, store } from '../store.js'

/* 登录 / 注册 / 管理员令牌三种进入方式；令牌是管理员引导路径（静态 API_TOKEN） */
const router = useRouter()
const mode = ref('login')
const username = ref('')
const password = ref('')
const displayName = ref('')
const orgTags = ref('')
const adminToken = ref('')
const error = ref(null)
const busy = ref(false)

async function submit() {
  if (busy.value) return
  error.value = null
  busy.value = true
  try {
    if (mode.value === 'token') {
      setToken(adminToken.value)
      await refreshMe()
      if (!store.authenticated) {
        throw new Error('令牌无效，或服务端未配置 API_TOKEN')
      }
    } else {
      const payload = { username: username.value.trim(), password: password.value }
      if (mode.value === 'register') {
        payload.display_name = displayName.value.trim() || payload.username
        payload.org_tags = orgTags.value.split(/[,，]/).map((s) => s.trim()).filter(Boolean)
      }
      await authenticate(payload, mode.value === 'register')
    }
    router.push('/console')
  } catch (e) {
    error.value = e
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="login-wrap">
    <div class="login-card panel">
      <span class="h-serif brand-line">检索台</span>
      <p class="eyebrow sub">管理后台</p>

      <div class="mode-switch">
        <button :disabled="busy" :class="{ active: mode === 'login' }" @click="mode = 'login'">登录</button>
        <button :disabled="busy" :class="{ active: mode === 'register' }" @click="mode = 'register'">注册</button>
        <button :disabled="busy" :class="{ active: mode === 'token' }" @click="mode = 'token'">管理员令牌</button>
      </div>

      <form @submit.prevent="submit">
        <template v-if="mode === 'token'">
          <label>
            <span class="fld-label">API_TOKEN（.env 中的静态管理员令牌）</span>
            <input v-model="adminToken" type="password" autocomplete="off" required />
          </label>
          <p class="token-hint">令牌即管理员身份：进入后可在「用户管理」把注册用户提升为管理员。</p>
        </template>
        <template v-else>
          <label>
            <span class="fld-label">用户名</span>
            <input v-model="username" type="text" autocomplete="username" required maxlength="64" />
          </label>
          <label>
            <span class="fld-label">密码</span>
            <input
              v-model="password"
              type="password"
              :autocomplete="mode === 'register' ? 'new-password' : 'current-password'"
              required
              :minlength="mode === 'register' ? 6 : 1"
              maxlength="100"
            />
          </label>
          <template v-if="mode === 'register'">
            <label>
              <span class="fld-label">显示名称</span>
              <input v-model="displayName" type="text" />
            </label>
            <label>
              <span class="fld-label">组织标签（逗号分隔，如：研发部, 市场部）</span>
              <input v-model="orgTags" type="text" placeholder="不填则无组织归属" />
            </label>
          </template>
        </template>
        <p v-if="error" class="error-note">{{ error.message || error }}</p>
        <button class="btn submit-btn" type="submit" :disabled="busy">
          {{ mode === 'login' ? '登录' : mode === 'register' ? '注册并登录' : '令牌进入' }}
        </button>
      </form>
    </div>
  </div>
</template>

<style scoped>
.login-wrap {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
}
.login-card { width: min(400px, calc(100vw - 32px)); padding: 28px 24px 22px; box-shadow: var(--shadow-md); }
.brand-line { font-size: 22px; font-weight: 600; display: block; }
.sub { margin: 2px 0 16px; }
/* Notion 分段控件 */
.mode-switch {
  display: flex;
  gap: 3px;
  margin-bottom: 18px;
  background: var(--neutral-bg);
  border-radius: var(--radius);
  padding: 3px;
}
.mode-switch button {
  flex: 1;
  border: 1px solid transparent;
  background: transparent;
  color: var(--text-2);
  font-weight: 500;
  padding: 5px 0;
  font-size: 13px;
  border-radius: var(--radius-sm);
  transition: background var(--t), color var(--t);
}
.mode-switch button:hover:not(.active) { background: var(--hover); color: var(--text); }
.mode-switch button.active {
  background: var(--bg);
  color: var(--text);
  font-weight: 600;
  border-color: var(--border);
  box-shadow: var(--shadow-sm);
}
form label { display: block; margin-bottom: 12px; }
.fld-label { display: block; font-size: 13px; font-weight: 600; margin-bottom: 4px; color: var(--text); }
.token-hint { font-size: 12px; color: var(--text-2); margin: -4px 0 10px; }
.submit-btn { width: 100%; margin-top: 6px; }
</style>
