import assert from 'node:assert/strict'
import { afterEach, test } from 'node:test'
import { api, ApiError, askStream, hasToken, setJwt } from '../src/api.js'

const storage = new Map()
globalThis.localStorage = {
  getItem: (key) => storage.get(key) ?? null,
  setItem: (key, value) => storage.set(key, String(value)),
  removeItem: (key) => storage.delete(key),
}
const originalFetch = globalThis.fetch
afterEach(() => {
  storage.clear()
  globalThis.fetch = originalFetch
})

test('conversation requests forward cancellation signals', async () => {
  const controller = new AbortController()
  const requests = []
  globalThis.fetch = async (url, options) => {
    assert.equal(options.signal, controller.signal)
    requests.push(url)
    return Response.json({})
  }
  await api.createConversation('测试', controller.signal)
  await api.getConversation(1, controller.signal)
  assert.deepEqual(requests, ['/api/v1/conversations', '/api/v1/conversations/1'])
})

test('streaming uses the same JWT authorization as ordinary requests', async () => {
  setJwt('user-jwt')
  globalThis.fetch = async (url, options) => {
    assert.equal(url, '/api/v1/qa/stream')
    assert.equal(options.headers.Authorization, 'Bearer user-jwt')
    assert.equal(options.headers['X-API-Token'], undefined)
    return new Response('event: done\ndata: {"status":"answered"}\n\n')
  }
  let result
  await askStream({ kb_id: 1, question: '问题' }, { onDone: (data) => { result = data } })
  assert.equal(result.status, 'answered')
})

test('legacy static tokens cannot authenticate requests', async () => {
  storage.set('rag.token', 'static-token')
  assert.equal(hasToken(), false)
  globalThis.fetch = async (url, options) => {
    assert.equal(options.headers['X-API-Token'], undefined)
    assert.equal(options.headers.Authorization, undefined)
    return new Response('event: done\ndata: {}\n\n')
  }
  await askStream({ kb_id: 1, question: '问题' })
})

test('user filters encode OR tag ids, keywords, roles and pagination', async () => {
  const controller = new AbortController()
  globalThis.fetch = async (url, options) => {
    const parsed = new URL(url, 'http://localhost')
    assert.equal(parsed.pathname, '/api/v1/admin/users')
    assert.equal(parsed.searchParams.get('keyword'), '研发 & 张')
    assert.equal(parsed.searchParams.get('is_admin'), 'false')
    assert.equal(parsed.searchParams.get('limit'), '20')
    assert.equal(parsed.searchParams.get('offset'), '20')
    assert.deepEqual(parsed.searchParams.getAll('org_tag_ids'), ['1', '2'])
    assert.equal(options.signal, controller.signal)
    return Response.json({ total: 0, items: [] })
  }
  await api.listUsers(20, 20, {
    keyword: '研发 & 张', isAdmin: false, orgTagIds: [1, 2],
  }, controller.signal)
})

test('user deletion uses DELETE and accepts the empty response', async () => {
  globalThis.fetch = async (url, options) => {
    assert.equal(url, '/api/v1/admin/users/3')
    assert.equal(options.method, 'DELETE')
    return new Response(null, { status: 204 })
  }
  assert.equal(await api.deleteUser(3), null)
})

test('login and registration send credentials without stale authorization', async () => {
  setJwt('expired-jwt')
  const paths = []
  globalThis.fetch = async (url, options) => {
    paths.push(url)
    assert.equal(options.headers.Authorization, undefined)
    assert.equal(JSON.parse(options.body).username, 'tester')
    return Response.json({ access_token: 'new-jwt' })
  }
  await api.login('tester', 'password')
  await api.register({ username: 'tester', password: 'password', display_name: '测试' })
  assert.deepEqual(paths, ['/api/v1/auth/login', '/api/v1/auth/register'])
})

test('authentication errors preserve the server message and request id', async () => {
  globalThis.fetch = async () => Response.json({
    code: 'unauthorized', message: '用户名或密码错误', request_id: 'req-1', retryable: false,
  }, { status: 401 })
  await assert.rejects(api.login('tester', 'wrong'), (error) => {
    assert.ok(error instanceof ApiError)
    assert.equal(error.message, '用户名或密码错误')
    assert.equal(error.requestId, 'req-1')
    return true
  })
})

test('SSE handles CRLF and UTF-8 split across network chunks', async () => {
  const bytes = new TextEncoder().encode('event: delta\r\ndata: {"text":"回答"}\r\n\r\nevent: done\r\ndata: {}\r\n\r\n')
  globalThis.fetch = async () => new Response(new ReadableStream({
    start(controller) {
      for (const byte of bytes) controller.enqueue(Uint8Array.of(byte))
      controller.close()
    },
  }))
  let answer = ''
  await askStream({}, { onDelta: (data) => { answer += data.text } })
  assert.equal(answer, '回答')
})

test('an interrupted SSE response rejects instead of leaving the UI streaming', async () => {
  globalThis.fetch = async () => new Response('event: delta\ndata: {"text":"部分回答"}\n\n')
  await assert.rejects(askStream({}), { code: 'stream_interrupted' })
})

test('readiness and network errors have predictable results', async () => {
  globalThis.fetch = async () => { throw new TypeError('Failed to fetch') }
  assert.equal(await api.readyz(), null)
  await assert.rejects(api.login('tester', 'password'), { code: 'network_error' })
})

test('admin endpoints hit /api/v1/admin with JSON bodies', async () => {
  setJwt('admin-jwt')
  const calls = []
  globalThis.fetch = async (url, options) => {
    calls.push({ url, method: options.method, body: options.body ? JSON.parse(options.body) : null })
    return Response.json({ items: [], total: 0 })
  }
  await api.listOrgTags()
  await api.createOrgTag('研发部')
  await api.renameOrgTag(3, '市场部')
  await api.deleteOrgTag(3)
  await api.listUsers()
  await api.updateUserAdmin(7, true)
  await api.updateUserTags(7, [3, 5])
  await api.updateDoc(42, { scope: '研发部', visibility: 'public' })
  assert.deepEqual(
    calls.map((c) => `${c.method} ${c.url}`),
    [
      'GET /api/v1/admin/org-tags',
      'POST /api/v1/admin/org-tags',
      'PATCH /api/v1/admin/org-tags/3',
      'DELETE /api/v1/admin/org-tags/3',
      'GET /api/v1/admin/users?limit=200&offset=0',
      'PATCH /api/v1/admin/users/7',
      'PUT /api/v1/admin/users/7/org-tags',
      'PATCH /api/v1/documents/42',
    ]
  )
  assert.deepEqual(calls[1].body, { name: '研发部' })
  assert.deepEqual(calls[5].body, { is_admin: true })
  assert.deepEqual(calls[6].body, { tag_ids: [3, 5] })
  assert.deepEqual(calls[7].body, { scope: '研发部', visibility: 'public' })
})

test('conversation endpoints use /api/v1/conversations', async () => {
  setJwt('user-jwt')
  const calls = []
  globalThis.fetch = async (url, options) => {
    calls.push(`${options.method} ${url}`)
    return Response.json({ total: 0, items: [], messages: [] })
  }
  await api.listConversations()
  await api.createConversation('报销期限')
  await api.getConversation(7)
  await api.deleteConversation(7)
  assert.deepEqual(calls, [
    'GET /api/v1/conversations?limit=20&offset=0',
    'POST /api/v1/conversations',
    'GET /api/v1/conversations/7',
    'DELETE /api/v1/conversations/7',
  ])
})

test('204 responses resolve to null without JSON parsing', async () => {
  setJwt('user-jwt')
  globalThis.fetch = async () => new Response(null, { status: 204 })
  assert.equal(await api.deleteConversation(7), null)
})
