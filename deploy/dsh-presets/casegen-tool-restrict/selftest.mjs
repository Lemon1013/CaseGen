import assert from 'node:assert/strict'
import { ALLOWED_TOOL_NAMES, apply, inject, name } from './casegen-tool-restrict-entry.mjs'

assert.equal(name, 'casegen-tool-restrict')
assert.deepEqual(inject, ['tools', 'agents', 'agentPresets'])

const presetByContext = new WeakMap()

function makeAgent(id, presetId) {
  const calls = {
    restrict: [],
    guard: [],
    installedEffects: [],
    disposedEffects: [],
    restrictDisposed: 0,
    guardDisposed: 0,
  }

  const agent = {
    id,
    ctx: {
      tools: {
        restrict(filter) {
          calls.restrict.push(filter)
          let active = true
          return () => {
            if (!active) return
            active = false
            calls.restrictDisposed += 1
          }
        },
        guard(fn) {
          calls.guard.push(fn)
          let active = true
          return () => {
            if (!active) return
            active = false
            calls.guardDisposed += 1
          }
        },
      },
      effect(fn, label) {
        const cleanup = fn()
        calls.installedEffects.push(label)
        let active = true
        return () => {
          if (!active) return
          active = false
          if (typeof cleanup === 'function') cleanup()
          calls.disposedEffects.push(label)
        }
      },
    },
    calls,
  }

  presetByContext.set(agent.ctx, presetId)
  return agent
}

const listeners = new Map()
const pluginEffects = []
let teamTryCalls = 0
const originalTeamTryMembership = function tryMembership(agent) {
  teamTryCalls += 1
  return agent?.id?.startsWith('casegen') ? { team: agent.id } : { team: 'other' }
}
const existingCasegen = makeAgent('agent-casegen-existing', 'casegen-testcase')
const existingOther = makeAgent('agent-other-existing', 'other-preset')

const ctx = {
  agents: {
    list() {
      return [existingCasegen, existingOther]
    },
  },
  agentPresets: {
    composedPreset(agentCtx) {
      return presetByContext.get(agentCtx)
    },
  },
  get(name) {
    return name === 'agentTeams' ? this.agentTeams : undefined
  },
  agentTeams: {
    tryMembership: originalTeamTryMembership,
  },
  on(event, listener) {
    const entries = listeners.get(event) ?? []
    entries.push(listener)
    listeners.set(event, entries)
    return () => {}
  },
  effect(fn, label) {
    const cleanup = fn()
    pluginEffects.push({ label, cleanup })
    return () => {
      if (typeof cleanup === 'function') cleanup()
    }
  },
}

apply(ctx)

assert.equal('tools' in ctx, false, '插件 apply 不得在全局 ctx 上注册 tools.restrict/guard')
assert.equal(existingCasegen.calls.restrict.length, 1, 'casegen-testcase 的已存在 agent 应安装 restrict')
assert.deepEqual(existingCasegen.calls.restrict[0], { allow: [...ALLOWED_TOOL_NAMES] })
assert.equal(existingCasegen.calls.guard.length, 1, 'casegen-testcase 的已存在 agent 应安装 guard')
assert.equal(existingOther.calls.restrict.length, 0, '其它 preset 的已存在 agent 不得安装 restrict')
assert.equal(existingOther.calls.guard.length, 0, '其它 preset 的已存在 agent 不得安装 guard')
assert.deepEqual(existingOther.calls.installedEffects, [], '其它 preset 不得产生 agent effect')
assert.equal(listeners.get('agent/created')?.length, 1, '应监听 agent/created')
assert.equal(listeners.get('agent/disposed')?.length, 1, '应监听 agent/disposed')
assert.equal(pluginEffects.length, 1, '插件应注册一个自身 effect 用于卸载清理')
assert.equal(pluginEffects[0].label, 'casegen-tool-restrict.plugin')

// agentTeams.tryMembership 包装：casegen agent 不可见 Team membership，其它 agent 保持原行为。
assert.notEqual(ctx.agentTeams.tryMembership, originalTeamTryMembership)
assert.equal(ctx.agentTeams.tryMembership(existingCasegen), undefined)
assert.deepEqual(ctx.agentTeams.tryMembership(existingOther), { team: 'other' })
assert.equal(teamTryCalls, 1, '只有其它 preset 的 tryMembership 会落到原方法')

const guard = existingCasegen.calls.guard[0]
for (const toolName of ['read', 'read_image', 'glob', 'grep', 'todo_write', 'skill']) {
  assert.equal(guard({ name: toolName }), undefined, toolName + ' 应放行')
}
for (const toolName of ['bash', 'write', 'edit', 'terminal_open', 'subagent', 'mcp__snowluma__qq_send_message']) {
  const reason = guard({ name: toolName })
  assert.equal(typeof reason, 'string', toolName + ' 应被拒绝')
  assert.match(reason, /不在 CaseGen 测试用例生产白名单内/)
}
assert.equal(guard(undefined), '工具名无效，已拒绝')
assert.equal(guard({ name: '' }), '工具名无效，已拒绝')

const createdCasegen = makeAgent('agent-casegen-created', 'casegen-testcase')
const createdOther = makeAgent('agent-other-created', 'other-preset')
for (const listener of listeners.get('agent/created')) {
  listener({ agent: createdCasegen })
  listener({ agent: createdOther })
}
assert.equal(createdCasegen.calls.restrict.length, 1, 'agent/created 的 casegen agent 应安装 restrict')
assert.deepEqual(createdCasegen.calls.restrict[0], { allow: [...ALLOWED_TOOL_NAMES] })
assert.equal(createdCasegen.calls.guard.length, 1, 'agent/created 的 casegen agent 应安装 guard')
assert.deepEqual(createdCasegen.calls.installedEffects, ['casegen-tool-restrict.agent'])
assert.equal(createdOther.calls.restrict.length, 0, 'agent/created 的其它 preset agent 不得安装 restrict')
assert.equal(createdOther.calls.guard.length, 0, 'agent/created 的其它 preset agent 不得安装 guard')
assert.deepEqual(createdOther.calls.installedEffects, [])

// 重复 announcement 不应重复安装。
for (const listener of listeners.get('agent/created')) {
  listener({ agent: createdCasegen })
  listener({ agent: createdOther })
}
assert.equal(createdCasegen.calls.restrict.length, 1)
assert.equal(createdCasegen.calls.guard.length, 1)
assert.equal(createdOther.calls.restrict.length, 0)
assert.equal(createdOther.calls.guard.length, 0)

for (const listener of listeners.get('agent/disposed')) {
  listener({ agent: createdCasegen })
  listener({ agent: createdOther })
}
assert.equal(createdCasegen.calls.restrictDisposed, 1, 'casegen agent dispose 应解除 restrict')
assert.equal(createdCasegen.calls.guardDisposed, 1, 'casegen agent dispose 应解除 guard')
assert.deepEqual(createdCasegen.calls.disposedEffects, ['casegen-tool-restrict.agent'])
assert.deepEqual(createdOther.calls.disposedEffects, [])

// 插件卸载应清理仍在的 casegen agent 级注册，且不碰其它 preset。
pluginEffects[0].cleanup()
assert.equal(existingCasegen.calls.restrictDisposed, 1, '插件卸载应解除已有 casegen agent 的 restrict')
assert.equal(existingCasegen.calls.guardDisposed, 1, '插件卸载应解除已有 casegen agent 的 guard')
assert.deepEqual(existingCasegen.calls.disposedEffects, ['casegen-tool-restrict.agent'])
assert.deepEqual(existingOther.calls.disposedEffects, [])
assert.equal(ctx.agentTeams.tryMembership, originalTeamTryMembership, '插件卸载应恢复 agentTeams.tryMembership')

// team service 异常（getter 抛错 / 方法不可写）只能跳过 team 抑制，不能影响插件加载。
function makeMinimalContext(agentTeamsFactory) {
  const agent = makeAgent('agent-casegen-minimal', 'casegen-testcase')
  const localEffects = []
  const localListeners = new Map()
  const localCtx = {
    agents: { list() { return [agent] } },
    agentPresets: { composedPreset(agentCtx) { return presetByContext.get(agentCtx) } },
    get(name) {
      return name === 'agentTeams' ? this.agentTeams : undefined
    },
    on(event, listener) {
      const entries = localListeners.get(event) ?? []
      entries.push(listener)
      localListeners.set(event, entries)
      return () => {}
    },
    effect(fn, label) {
      const cleanup = fn()
      localEffects.push({ label, cleanup })
      return () => { if (typeof cleanup === 'function') cleanup() }
    },
  }
  if (agentTeamsFactory !== undefined) localCtx.agentTeams = agentTeamsFactory()
  return { agent, ctx: localCtx }
}

const getterThrow = makeMinimalContext()
Object.defineProperty(getterThrow.ctx, 'agentTeams', {
  configurable: true,
  get() { throw new Error('agentTeams unavailable') },
})
assert.doesNotThrow(() => apply(getterThrow.ctx), 'agentTeams getter 抛错时 apply 不应失败')
assert.equal(getterThrow.agent.calls.restrict.length, 1, 'team 服务异常时 restrict 仍应安装')
assert.equal(getterThrow.agent.calls.guard.length, 1, 'team 服务异常时 guard 仍应安装')

const frozenTeam = makeMinimalContext(() => Object.freeze({ tryMembership() { return { team: 'x' } } }))
assert.doesNotThrow(() => apply(frozenTeam.ctx), 'agentTeams.tryMembership 不可写时 apply 不应失败')
assert.equal(frozenTeam.agent.calls.restrict.length, 1, 'team 方法不可写时 restrict 仍应安装')
assert.equal(frozenTeam.agent.calls.guard.length, 1, 'team 方法不可写时 guard 仍应安装')

assert.deepEqual([...ALLOWED_TOOL_NAMES].sort(), ['glob', 'grep', 'read', 'read_image', 'skill', 'todo_write'])
console.log('casegen-tool-restrict selftest: ok')
