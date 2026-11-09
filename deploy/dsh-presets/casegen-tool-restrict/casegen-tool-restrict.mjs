// CaseGen「测试用例生产」preset 的工具收紧守卫。
//
// 该 preset 只需要只读查阅知识空间与调用专家技能，不应当拥有继承自全局层的
// 任何 MCP、shell、终端、子代理、团队、会话、插件管理等能力。
//
// DSH 0.2.1 起 tools.restrict() / tools.guard() 必须注册在 agent scoped context 上：
// 在插件 apply 的全局 ctx 上调用 restrict 会抛
// "tools.restrict() requires a scoped context (agent.ctx): ..."，导致插件 never started。
// 因此改为在每个 agent 的 agent/created 事件里安装两层防线：
//  1) agent.ctx.tools.restrict({ allow: [...ALLOWED_TOOL_NAMES] })：只保留六工具白名单；
//     它只过滤“继承面”，不影响 preset 自己 scoped 注册的 read/read_image/glob/
//     grep/todo_write/skill，因此无需逐个列举 71 个 MCP 工具名。
//  2) agent.ctx.tools.guard(...)：执行期白名单兜底，任何非上述六种工具一律拒绝。
//
// 所有 agent 级 disposer 都包在一个 agent.ctx.effect() 中，并按 agent 记录在插件
// 自己的 Map 里：agent dispose 或插件卸载时都会清理，避免泄漏。
//
// 注意：插件行名称必须是从 profile 目录可解析的裸包说明符
// '@local/dsh-casegen-tool-restrict/casegen-tool-restrict-entry.mjs'，不能写成相对路径。
export const name = 'casegen-tool-restrict'

export const inject = ['tools', 'agents', 'agentPresets']

const ALLOWED_TOOL_NAMES = new Set([
  'read',
  'read_image',
  'glob',
  'grep',
  'todo_write',
  'skill',
])

export { ALLOWED_TOOL_NAMES }

function createGuard() {
  return (exec) => {
    const toolName = exec?.name
    if (typeof toolName !== 'string' || toolName.length === 0) {
      return '工具名无效，已拒绝'
    }
    if (ALLOWED_TOOL_NAMES.has(toolName)) {
      return
    }
    return `工具 "${toolName}" 不在 CaseGen 测试用例生产白名单内，已拒绝`
  }
}

export function apply(ctx) {
  const installed = new Map()

  const install = (agent) => {
    if (installed.has(agent)) return
    // preset 的插件树挂在 registry 共享 scope 下，而 agent/created 与
    // ctx.agents.list() 都是全局事件/全局列表；必须只对绑定了本 preset 的
    // agent 安装，否则会把限制装到 Lead 等其它 preset 的会话上。
    if (ctx.agentPresets.composedPreset(agent.ctx) !== 'casegen-testcase') return

    const dispose = agent.ctx.effect(() => {
      const disposeRestrict = agent.ctx.tools.restrict({ allow: [...ALLOWED_TOOL_NAMES] })
      const disposeGuard = agent.ctx.tools.guard(createGuard())
      return () => {
        disposeGuard()
        disposeRestrict()
      }
    }, 'casegen-tool-restrict.agent')

    installed.set(agent, dispose)
  }

  // 覆盖插件加载时已经存在的 agent（例如 profile 热重载）。
  for (const agent of ctx.agents.list()) {
    install(agent)
  }

  // agent-team 的 9 个工具是 @deepseek-ai/dsh-experimental-tool-agent-team
  // 在 agent.ctx own-scope 上注册的，tools.restrict 只过滤继承面，无法隐藏。
  // 对 casegen-testcase agent 让 tryMembership 返回 undefined，使 team 插件
  // 的 maybeInstall 跳过该 agent；其它 agent 保持原行为。
  //
  // 整段 team patch 都包在 try/catch 里：agentTeams 服务不存在、getter 抛错或
  // 方法不可写时只跳过 team 抑制，restrict + guard 仍然照常安装，绝不能让插件
  // 因 team 服务差异而加载失败。
  let restoreTeamMembership = () => {}
  try {
    const teamService =
      (typeof ctx.get === 'function' ? ctx.get('agentTeams') : undefined) ?? ctx.agentTeams
    const originalTryMembership = teamService?.tryMembership
    if (typeof originalTryMembership === 'function') {
      const patchedTryMembership = function tryMembership(agent) {
        if (agent && ctx.agentPresets.composedPreset(agent.ctx) === 'casegen-testcase') {
          return undefined
        }
        return originalTryMembership.call(teamService, agent)
      }
      teamService.tryMembership = patchedTryMembership
      restoreTeamMembership = () => {
        try {
          if (teamService.tryMembership === patchedTryMembership) {
            teamService.tryMembership = originalTryMembership
          }
        } catch {
          // 恢复失败最多把抑制保留到进程重启，不能影响插件卸载。
        }
      }
    }
  } catch {
    // agentTeams 不可用/不可写：跳过 team 抑制，restrict/guard 继续。
  }

  ctx.on('agent/created', ({ agent }) => {
    install(agent)
  })

  ctx.on('agent/disposed', ({ agent }) => {
    const dispose = installed.get(agent)
    if (dispose === undefined) return
    installed.delete(agent)
    dispose()
  })

  // 插件卸载时清理所有尚未随 agent 销毁的 agent 级注册，并恢复 team 服务方法。
  ctx.effect(() => () => {
    for (const dispose of installed.values()) {
      dispose()
    }
    installed.clear()
    restoreTeamMembership()
  }, 'casegen-tool-restrict.plugin')
}