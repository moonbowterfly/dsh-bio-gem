/**
 * Verify that optional webServer support never blocks gem tool registration.
 *
 * Run: node --import ./test/register-dsh-tools.mjs test/optional-injection.js
 */
import assert from 'node:assert/strict'

const plugin = await import('../src/index.js')

assert.deepEqual(plugin.inject, {
  required: ['tools', 'skills'],
  optional: ['webServer'],
})

let toolRegistrations = 0
let skillRegistrations = 0
let effect = null
const routes = []
const ctx = {
  tools: { register: () => { toolRegistrations += 1; return () => {} } },
  skills: { register: () => { skillRegistrations += 1; return () => {} } },
  effect: (callback) => {
    effect = callback
    return callback()
  },
}

plugin.apply(ctx)
assert.equal(toolRegistrations, 21)
assert.equal(skillRegistrations, 1)
assert.equal(routes.length, 0)

ctx.webServer = {
  register: (route) => {
    routes.push(route)
    return () => routes.splice(routes.indexOf(route), 1)
  },
}
effect()
assert.deepEqual(routes.map((route) => route.path), [
  '/api/dsh-bio-gem/integration/health',
  '/api/dsh-bio-gem/integration/v1/status',
])

console.log('✓ optional webServer leaves all 21 gem tools active')
