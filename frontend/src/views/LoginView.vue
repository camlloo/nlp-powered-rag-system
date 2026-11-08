<script setup>
import { computed, nextTick, ref } from 'vue'
import { useRouter } from 'vue-router'
import { authenticate } from '../store.js'

const router = useRouter()
const mode = ref('login')
const isRegister = computed(() => mode.value === 'register')
const username = ref('')
const password = ref('')
const displayName = ref('')
const showPassword = ref(false)
const error = ref(null)
const busy = ref(false)
const usernamePattern = '[a-zA-Z0-9_.\\-]+'

function switchMode(nextMode) {
  if (busy.value || nextMode === mode.value) return
  mode.value = nextMode
  password.value = ''
  showPassword.value = false
  error.value = null
}

function onTabKeydown(event) {
  if (busy.value || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
  event.preventDefault()
  let nextMode = isRegister.value ? 'login' : 'register'
  if (event.key === 'Home') nextMode = 'login'
  if (event.key === 'End') nextMode = 'register'
  switchMode(nextMode)
  nextTick(() => document.getElementById(`auth-${nextMode}-tab`)?.focus())
}

async function submit() {
  if (busy.value) return
  error.value = null
  busy.value = true
  try {
    const payload = { username: username.value.trim(), password: password.value }
    if (isRegister.value) {
      payload.display_name = displayName.value.trim() || payload.username
    }
    await authenticate(payload, isRegister.value)
    await router.push('/console')
  } catch (err) {
    error.value = err
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="auth-page">
    <aside class="brand-side" aria-label="检索台介绍">
      <div class="brand">
        <svg class="brand-mark" viewBox="0 0 32 36" fill="none" aria-hidden="true">
          <path d="M8 8H5a3 3 0 0 0-3 3v20a3 3 0 0 0 3 3h17a3 3 0 0 0 3-3v-3" />
          <path d="M11 2h12l7 7v16a3 3 0 0 1-3 3H11a3 3 0 0 1-3-3V5a3 3 0 0 1 3-3Z" />
          <path d="M22 2v8h8M14 16h10M14 21h7" />
        </svg>
        <span>检索台</span>
      </div>

      <div class="brand-content">
        <h2>把文档，<br />变成可查证的答案。</h2>
        <p class="brand-description">连接文档与问题，让每个回答都有出处。</p>
        <figure class="document-demo">
          <figcaption>功能示意</figcaption>
          <div class="source-paper">
            <div class="paper-title">
              <svg viewBox="0 0 24 28" fill="none" aria-hidden="true">
                <path d="M4 1h10l7 7v18H4V1Z" />
                <path d="M14 1v8h7M8 14h9M8 19h7" />
              </svg>
              <span>项目文档.md</span>
            </div>
            <p class="source-excerpt">
              <mark>原文是回答的依据，引用让信息可以核验。</mark>
              <span class="source-marker">S1</span>
            </p>
            <div class="document-lines" aria-hidden="true"><span></span><span></span></div>
          </div>
          <div class="answer-paper">
            <span class="answer-label">回答</span>
            <p>基于可见文档作答，并附上原文引用。<span class="answer-source">[S1]</span></p>
          </div>
        </figure>
      </div>
      <p class="brand-footer">文档检索 · 多轮问答 · 原文引用</p>
    </aside>

    <main class="form-side">
      <div class="auth-content">
        <header class="auth-heading">
          <h1>{{ isRegister ? '创建账号' : '欢迎回来' }}</h1>
          <p>{{ isRegister ? '开始整理与探索你的文档。' : '登录后，继续你的文档问答。' }}</p>
        </header>

        <div class="auth-tabs" role="tablist" aria-label="账号访问方式" @keydown="onTabKeydown">
          <button
            id="auth-login-tab"
            type="button"
            role="tab"
            aria-controls="auth-panel"
            :aria-selected="!isRegister"
            :tabindex="isRegister ? -1 : 0"
            :class="{ active: !isRegister }"
            :disabled="busy"
            @click="switchMode('login')"
          >登录</button>
          <button
            id="auth-register-tab"
            type="button"
            role="tab"
            aria-controls="auth-panel"
            :aria-selected="isRegister"
            :tabindex="isRegister ? 0 : -1"
            :class="{ active: isRegister }"
            :disabled="busy"
            @click="switchMode('register')"
          >注册</button>
        </div>

        <div id="auth-panel" role="tabpanel" :aria-labelledby="`auth-${mode}-tab`">
          <form class="auth-form" :aria-busy="busy" @submit.prevent="submit">
            <div class="auth-field">
              <label for="auth-username">用户名</label>
              <input
                id="auth-username"
                v-model="username"
                class="auth-input"
                name="username"
                type="text"
                autocomplete="username"
                autocapitalize="none"
                spellcheck="false"
                required
                :minlength="isRegister ? 3 : 1"
                maxlength="64"
                :pattern="isRegister ? usernamePattern : undefined"
                :title="isRegister ? '3–64 位字母、数字或下划线、点、短横线' : undefined"
                :placeholder="isRegister ? '3–64 位字母、数字或 _ . -' : '输入用户名'"
                :disabled="busy"
                @input="error = null"
              />
            </div>

            <div v-if="isRegister" class="auth-field">
              <label for="auth-display-name">显示名称 <span class="optional-label">选填</span></label>
              <input
                id="auth-display-name"
                v-model="displayName"
                class="auth-input"
                name="display_name"
                type="text"
                autocomplete="nickname"
                maxlength="100"
                placeholder="默认使用用户名"
                :disabled="busy"
                @input="error = null"
              />
            </div>

            <div class="auth-field">
              <label for="auth-password">密码</label>
              <div class="password-input">
                <input
                  id="auth-password"
                  v-model="password"
                  class="auth-input"
                  name="password"
                  :type="showPassword ? 'text' : 'password'"
                  :autocomplete="isRegister ? 'new-password' : 'current-password'"
                  required
                  :minlength="isRegister ? 6 : 1"
                  maxlength="100"
                  :placeholder="isRegister ? '至少 6 位，最多 100 位' : '输入密码'"
                  :disabled="busy"
                  @input="error = null"
                />
                <button
                  class="password-toggle"
                  type="button"
                  :aria-label="showPassword ? '隐藏密码' : '显示密码'"
                  :aria-pressed="showPassword"
                  aria-controls="auth-password"
                  :disabled="busy"
                  @click="showPassword = !showPassword"
                >
                  <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
                    <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z" />
                    <circle cx="12" cy="12" r="3" />
                    <path v-if="showPassword" d="m3 3 18 18" />
                  </svg>
                </button>
              </div>
            </div>

            <div v-if="error" class="auth-error" role="alert">
              <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
                <circle cx="12" cy="12" r="9" /><path d="M12 7v6M12 16v1" />
              </svg>
              <p>{{ error.message || '暂时无法完成请求，请稍后重试。' }}</p>
            </div>

            <button class="auth-submit" type="submit" :disabled="busy">
              <span v-if="busy" class="submit-spinner" aria-hidden="true"></span>
              <span v-if="busy">{{ isRegister ? '正在创建账号…' : '正在登录…' }}</span>
              <span v-else>{{ isRegister ? '创建账号并登录' : '登录' }}</span>
              <svg v-if="!busy" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                <path d="M4 12h15m-6-6 6 6-6 6" />
              </svg>
            </button>
          </form>
          <p class="account-note">账号权限及组织成员身份由管理员管理。</p>
        </div>
      </div>
    </main>
  </div>
</template>

<style scoped>
.auth-page {
  --auth-ink: #172b4d;
  --auth-muted: #5c6f88;
  --auth-blue: #2463eb;
  --auth-line: #d6e1ef;
  display: grid;
  grid-template-columns: 48% minmax(0, 1fr);
  min-height: 100svh;
  color: var(--auth-ink);
  background: #fff;
}

.auth-page ::selection {
  background: #cde1ff;
}

.brand-side {
  display: flex;
  flex-direction: column;
  min-width: 0;
  padding: 40px clamp(32px, 4.5vw, 72px) 32px;
  background: #eef4fc;
}

.brand {
  display: flex;
  align-items: center;
  gap: 14px;
  font-size: 25px;
  font-weight: 700;
}

.brand-mark {
  width: 30px;
  height: 36px;
  flex: none;
}

.brand-mark, .paper-title svg {
  stroke: currentColor;
  stroke-width: 1.8;
  stroke-linejoin: round;
  stroke-linecap: round;
}

.brand-content {
  display: flex;
  flex: 1;
  flex-direction: column;
  justify-content: center;
  padding: 48px 0;
  width: 100%;
  max-width: 560px;
  margin: 0 auto;
}

.brand-content h2 {
  margin: 0;
  font-size: clamp(34px, 3.35vw, 54px);
  font-weight: 700;
  line-height: 1.45;
  letter-spacing: -0.035em;
  text-wrap: balance;
}

.brand-description {
  margin: 20px 0 0;
  color: var(--auth-muted);
  font-size: 16px;
  line-height: 1.8;
}

.document-demo {
  margin: 48px 0 0;
}

.document-demo figcaption {
  margin-bottom: 16px;
  font-size: 14px;
  font-weight: 600;
}

.source-paper, .answer-paper {
  background: #fff;
  border: 1px solid var(--auth-line);
  border-radius: 10px;
  box-shadow: 0 8px 24px rgb(23 43 77 / 6%);
}

.source-paper {
  padding: 20px 22px 42px;
}

.paper-title {
  display: flex;
  align-items: center;
  gap: 10px;
  padding-bottom: 14px;
  border-bottom: 1px solid var(--auth-line);
  font-size: 15px;
  font-weight: 600;
}

.paper-title svg {
  width: 21px;
  height: 25px;
  color: var(--auth-muted);
}

.source-excerpt {
  margin: 22px 0 16px;
  font-size: 14px;
  line-height: 2;
}

.source-excerpt mark {
  background: #dcecff;
  color: var(--auth-ink);
  padding: 2px 3px;
  box-decoration-break: clone;
  -webkit-box-decoration-break: clone;
}

.source-marker {
  color: var(--auth-blue);
  font-size: 12px;
  padding: 2px 4px;
  background: #edf4ff;
  margin-left: 4px;
}

.document-lines {
  display: grid;
  gap: 9px;
}

.document-lines span {
  height: 6px;
  width: 85%;
  background: #e2e9f2;
  border-radius: 2px;
}

.document-lines span:last-child {
  width: 65%;
}

.answer-paper {
  position: relative;
  margin: -22px 0 0 32px;
  padding: 16px 20px;
}

.answer-label {
  display: block;
  padding-bottom: 10px;
  border-bottom: 1px solid var(--auth-line);
  font-size: 14px;
  font-weight: 600;
}

.answer-paper p {
  margin: 12px 0 0;
  font-size: 13px;
  line-height: 1.9;
}

.answer-source {
  color: var(--auth-blue);
  white-space: nowrap;
  margin-left: 6px;
}

.brand-footer {
  margin: 0;
  color: var(--auth-muted);
  font-size: 12px;
}

.form-side {
  display: flex;
  align-items: center;
  justify-content: center;
  min-width: 0;
  padding: 56px 40px;
}

.auth-content {
  width: 100%;
  max-width: 384px;
}

.auth-heading h1 {
  margin: 0;
  font-size: 32px;
  font-weight: 700;
  line-height: 1.4;
  letter-spacing: -0.025em;
}

.auth-heading p {
  margin: 12px 0 0;
  color: var(--auth-muted);
  font-size: 14px;
  line-height: 1.8;
}

.auth-tabs {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  margin: 32px 0 28px;
  border-bottom: 1px solid var(--auth-line);
}

.auth-tabs button {
  min-height: 48px;
  padding: 10px 16px;
  margin-bottom: -1px;
  border: 0;
  border-bottom: 3px solid transparent;
  background: transparent;
  color: var(--auth-muted);
  font-size: 15px;
  font-weight: 500;
  transition: color 150ms, background 150ms;
}

.auth-tabs button:hover {
  color: var(--auth-blue);
  background: #f6f9ff;
}

.auth-tabs button.active {
  border-bottom-color: var(--auth-blue);
  color: var(--auth-blue);
  font-weight: 600;
}

.auth-tabs button:disabled {
  cursor: wait;
}

.auth-field {
  margin-bottom: 22px;
}

.auth-field label {
  display: block;
  margin-bottom: 9px;
  font-size: 14px;
  font-weight: 600;
}

.optional-label {
  margin-left: 5px;
  color: var(--auth-muted);
  font-size: 12px;
  font-weight: 400;
}

.auth-input {
  width: 100%;
  height: 50px;
  padding: 12px 14px;
  border: 1px solid #cbd8e9;
  border-radius: 8px;
  background: #fff;
  color: var(--auth-ink);
  font-family: inherit;
  font-size: 14px;
  line-height: 1.5;
  caret-color: var(--auth-blue);
  box-shadow: none;
  transition: border-color 150ms;
}

.auth-input::placeholder {
  color: #66768d;
  opacity: 1;
}

.auth-input:hover {
  border-color: #9fb5d2;
}

.auth-input:focus-visible {
  border-color: var(--auth-blue);
  outline: 3px solid #dce8ff;
  outline-offset: 2px;
  box-shadow: none;
}

.auth-input:disabled {
  background: #f6f8fc;
  color: var(--auth-muted);
  cursor: wait;
}

.password-input {
  position: relative;
}

.password-input .auth-input {
  padding-right: 54px;
}

.password-toggle {
  position: absolute;
  top: 5px;
  right: 7px;
  display: grid;
  place-items: center;
  width: 40px;
  height: 40px;
  border: 0;
  border-radius: 5px;
  background: transparent;
  color: var(--auth-muted);
}

.password-toggle:hover {
  color: var(--auth-ink);
  background: #eef4fc;
}

.password-toggle:disabled {
  cursor: wait;
}

.password-toggle svg, .auth-submit svg, .auth-error svg {
  width: 20px;
  height: 20px;
  stroke: currentColor;
  stroke-width: 1.8;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.auth-error {
  display: flex;
  align-items: start;
  gap: 9px;
  padding: 12px 14px;
  margin: 0 0 20px;
  background: #fff2f1;
  color: #a32d34;
  border-radius: 8px;
}

.auth-error svg {
  flex: none;
  margin-top: 1px;
}

.auth-error p {
  margin: 0;
  font-size: 13px;
  line-height: 1.7;
  overflow-wrap: anywhere;
}

.auth-submit {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 12px;
  width: 100%;
  min-height: 50px;
  margin-top: 30px;
  padding: 12px 18px;
  border: 0;
  border-radius: 8px;
  background: var(--auth-blue);
  color: #fff;
  font-size: 15px;
  font-weight: 600;
  line-height: 1.5;
  transition: background 150ms;
}

.auth-submit:hover {
  background: #1b51cc;
}

.auth-submit:active {
  background: #1846af;
}

.auth-submit:disabled {
  background: #4b75d0;
  cursor: wait;
}

.auth-page button:focus-visible {
  outline: 3px solid #a8c5ff;
  outline-offset: 3px;
}

.submit-spinner {
  width: 17px;
  height: 17px;
  border: 2px solid rgb(255 255 255 / 45%);
  border-top-color: #fff;
  border-radius: 50%;
  animation: submit-spin 800ms linear infinite;
}

.account-note {
  margin: 26px 0 0;
  color: var(--auth-muted);
  font-size: 12px;
  line-height: 1.8;
}

@keyframes submit-spin {
  to {
    transform: rotate(360deg);
  }
}

@media (max-width: 1100px) {
  .brand-side {
    padding: 32px;
  }
  .brand-content h2 {
    font-size: 35px;
  }
  .brand-description {
    font-size: 14px;
  }
  .source-paper {
    padding: 18px 18px 38px;
  }
  .source-excerpt {
    font-size: 13px;
  }
  .answer-paper {
    margin-left: 20px;
    padding: 14px 16px;
  }
  .form-side {
    padding: 48px 32px;
  }
}

@media (max-width: 800px) {
  .auth-page {
    grid-template-columns: minmax(0, 1fr);
    align-content: start;
  }
  .brand-side {
    padding: 24px;
    background: #fff;
    border-bottom: 1px solid #e7edf5;
  }
  .brand {
    gap: 11px;
    font-size: 22px;
  }
  .brand-mark {
    width: 25px;
    height: 30px;
  }
  .brand-content, .brand-footer {
    display: none;
  }
  .form-side {
    align-items: start;
    padding: 40px 24px 48px;
  }
  .auth-content {
    max-width: 420px;
  }
  .auth-heading h1 {
    font-size: 28px;
  }
  .auth-tabs {
    margin-top: 26px;
  }
}
</style>
