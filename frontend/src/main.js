import { createApp } from 'vue'
import App from './App.vue'
import { router } from './router.js'
import { refreshMe } from './store.js'
import './style.css'

/* 先恢复会话（/auth/me），路由守卫的 admin 判断才有 currentUser 可依据 */
async function bootstrap() {
  try {
    await refreshMe()
  } catch {
    /* 令牌失效时 refreshMe 已清理并跳登录 */
  }
  createApp(App).use(router).mount('#app')
}

bootstrap()
