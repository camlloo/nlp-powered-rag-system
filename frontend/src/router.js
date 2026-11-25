/* 路由：hash 模式（后端 SPA 同源托管无需回退配置）。
   /login 独立页；其余都在传统后台壳 AdminLayout 内。 */

import { createRouter, createWebHashHistory } from 'vue-router'
import { hasToken } from './api.js'
import AdminLayout from './layouts/AdminLayout.vue'
import { isAdmin } from './store.js'
import ConsoleView from './views/ConsoleView.vue'
import DocManageView from './views/DocManageView.vue'
import EvalDashboardView from './views/EvalDashboardView.vue'
import LoginView from './views/LoginView.vue'
import OrgTagView from './views/OrgTagView.vue'
import SettingsView from './views/SettingsView.vue'
import UserView from './views/UserView.vue'

export const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/login', name: 'login', component: LoginView },
    {
      path: '/',
      component: AdminLayout,
      children: [
        { path: '', redirect: '/console' },
        { path: 'console', name: 'console', component: ConsoleView, meta: { title: '任务' } },
        { path: 'docs', name: 'docs', component: DocManageView, meta: { title: '文档管理' } },
        { path: 'org-tags', name: 'org-tags', component: OrgTagView, meta: { title: '组织标签', admin: true } },
        { path: 'eval', name: 'eval', component: EvalDashboardView, meta: { title: '模型评测', admin: true } },
        { path: 'users', name: 'users', component: UserView, meta: { title: '用户管理', admin: true } },
        { path: 'settings', name: 'settings', component: SettingsView, meta: { title: '系统设置' } },
      ],
    },
    { path: '/:pathMatch(.*)*', redirect: '/console' },
  ],
})

router.beforeEach((to) => {
  if (to.name === 'login') return hasToken() ? { path: '/console' } : true
  if (!hasToken()) return { path: '/login' }
  if (to.meta.admin && !isAdmin.value) return { path: '/console' }
  return true
})
