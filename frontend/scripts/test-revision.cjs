const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const assert = require('node:assert/strict')
const ts = require('typescript')

const code = fs.readFileSync(path.join(__dirname, '../src/services/revision.ts'), 'utf8')
const compiled = ts.transpileModule(code, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } }).outputText
const context = { exports: {} }
vm.runInNewContext(compiled, context)
const failure = context.exports.revisionFailure
assert.equal(failure({ revision_status: 'rejected', revision_message: '版本冲突' }), '版本冲突')
assert.ok(failure({ revision_status: 'unchanged' }))
assert.ok(failure({ revision_status: 'clarify_needed', revision_message: '请注明第几天' }))
assert.equal(failure({ revision_status: 'draft_ready', warnings: [] }), null)
assert.equal(failure({ revision_status: 'applied', warnings: ['本次自然语言修改未通过结构校验，已保留原计划。'] }), null)
assert.ok(failure({ warnings: ['本次自然语言修改未通过结构校验，已保留原计划。'] }))
assert.equal(failure({ warnings: ['天气预报有雨，请携带雨具'] }), null)
assert.equal(failure({}), null)
// 手动编辑的组合操作顺序必须固定，避免跨日移动后重排索引偏移。
const manual = context.exports.manualRevisionOperations
const place = (id) => ({ stable_id: id, name: id, visit_duration: 60, description: '介绍' })
const before = { days: [{ attractions: [place('A'), place('B'), place('C')] }, { attractions: [place('D')] }] }
const after = { days: [{ attractions: [place('B')] }, { attractions: [{ ...place('A'), visit_duration: 75, description: '新介绍' }, place('D')] }] }
const operations = JSON.parse(JSON.stringify(manual(before, after)))
assert.deepEqual(operations.map(op => op.op), ['remove_place', 'move_place', 'reorder_day', 'reorder_day', 'change_duration', 'update_description'])
assert.equal(operations[0].target_stable_id, 'C')
assert.equal(operations[1].day_index, 1)
assert.deepEqual(operations[3].ordered_stable_ids, ['A', 'D'])
assert.equal(operations[4].duration_minutes, 75)
assert.equal(manual(before, before).length, 0)
assert.throws(() => manual(before, { days: [{ attractions: [] }, before.days[1]] }), /至少保留/)
assert.throws(() => manual(before, { days: [{ attractions: [place('X')] }, before.days[1]] }), /添加地点/)
console.log('Revision result guards passed: rejection, no-op, legacy drafts and historical warnings.')
