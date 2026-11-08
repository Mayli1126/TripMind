// Lightweight regressions with Node's built-in assertions; no test dependency added.
const assert = require('node:assert/strict')
const fs = require('node:fs')
const vm = require('node:vm')
const ts = require('typescript')
const webcrypto = require('node:crypto').webcrypto
const calls = []
const api = { put: async (url, payload) => { calls.push({ url, payload }); return { data: { revision: payload.revision + 1 } } } }
const moduleObject = { exports: {} }
const source = ts.transpileModule(fs.readFileSync('src/services/conversations.ts', 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } }).outputText
vm.runInNewContext(source, { exports: moduleObject.exports, module: moduleObject, crypto: { getRandomValues: webcrypto.getRandomValues.bind(webcrypto) }, Uint8Array,
  require(name) { if (name === 'vue') return { ref: value => ({ value }) }; if (name === './api') return { apiClient: api }; throw new Error(name) } })
const { newConversationId, useConversation } = moduleObject.exports
;(async () => {
  const ids = new Set(Array.from({ length: 100 }, () => newConversationId()))
  assert.equal(ids.size, 100)
  for (const id of ids) assert.match(id, /^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/)
  const cloud = useConversation('planner')
  await Promise.all([cloud.save({ messages: ['first'] }, 'Trip'), cloud.save({ messages: ['first', 'second'] }, 'Trip')])
  assert.deepEqual(calls.map(c => c.payload.revision), [0, 1])
  assert.equal(cloud.identity().revision, 2)
  const oldId = cloud.identity().id
  cloud.reset()
  assert.notEqual(cloud.identity().id, oldId)
  assert.equal(cloud.identity().revision, 0)
  api.put = async () => { throw { response: { status: 409, data: { detail: 'conflict' } } } }
  await cloud.save({}, 'Conflicting')
  assert.equal(cloud.syncMessage.value, 'conflict')
  assert.equal(cloud.identity().revision, 0)
  console.log('PASS: HTTP UUID fallback, uniqueness, queued revisions, new-session reset, conflict preservation')
})().catch(error => { console.error(error); process.exitCode = 1 })
