import type { DraftCaseItem, DraftPointGroup } from '../../api/tasks'

/**
 * 从 assistant 消息正文提取用例 JSON（spec §3.5 输出契约：json fenced block，
 * DraftPointGroup[] / DraftCaseItem[] 同构）。解析失败返回 null，由调用方提示导出 Markdown。
 */

const JSON_FENCE_PATTERN = /```json\s*([\s\S]*?)```/gi

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value)
}

function asString(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback
}

function normalizeCase(raw: unknown, index: number): DraftCaseItem | null {
  if (!isRecord(raw)) return null
  const rawSteps = Array.isArray(raw.steps) ? raw.steps : []
  const steps = rawSteps
    .map((step, stepIndex) => {
      if (!isRecord(step)) return null
      const stepNo = Number(step.step_no)
      return {
        step_no: Number.isFinite(stepNo) ? stepNo : stepIndex + 1,
        action: asString(step.action),
        expected: asString(step.expected),
      }
    })
    .filter((step): step is { step_no: number; action: string; expected: string } => step !== null)
  return {
    case_key: asString(raw.case_key, `TC-${String(index + 1).padStart(3, '0')}`),
    title: asString(raw.title),
    priority: asString(raw.priority, 'P2'),
    type: asString(raw.type),
    verification_goal: asString(raw.verification_goal),
    preconditions: asString(raw.preconditions),
    test_data: asString(raw.test_data),
    steps,
    pending_items: Array.isArray(raw.pending_items)
      ? raw.pending_items.map((item) => asString(item)).filter(Boolean)
      : [],
    raw_md: asString(raw.raw_md),
  }
}

function normalizeGroup(raw: unknown, index: number): DraftPointGroup | null {
  if (!isRecord(raw)) return null
  const rawCases = Array.isArray(raw.cases) ? raw.cases : []
  const cases = rawCases
    .map((item, caseIndex) => normalizeCase(item, caseIndex))
    .filter((item): item is DraftCaseItem => item !== null)
  if (!cases.length) return null
  const testPointId = Number(raw.test_point_id)
  return {
    test_point_id: Number.isInteger(testPointId) ? testPointId : null,
    stable_key: asString(raw.stable_key, `agent-point-${index + 1}`),
    title: asString(raw.title),
    verification_goal: asString(raw.verification_goal),
    dimension: asString(raw.dimension),
    priority: asString(raw.priority, 'P2'),
    citation_ids: Array.isArray(raw.citation_ids)
      ? raw.citation_ids.map((id) => Number(id)).filter((id) => Number.isInteger(id))
      : [],
    cases,
  }
}

/** 解析一段正文里的全部 json fenced block，返回可渲染的 DraftPointGroup[]；无有效结构返回 null。 */
export function parseAgentCaseGroups(content: string): DraftPointGroup[] | null {
  if (!content) return null
  const groups: DraftPointGroup[] = []
  let matched = false
  for (const block of content.matchAll(JSON_FENCE_PATTERN)) {
    const body = block[1]?.trim()
    if (!body) continue
    let parsed: unknown
    try {
      parsed = JSON.parse(body)
    } catch {
      continue
    }
    const candidates = Array.isArray(parsed)
      ? parsed
      : isRecord(parsed) && Array.isArray(parsed.points_with_cases)
        ? parsed.points_with_cases
        : isRecord(parsed) && Array.isArray(parsed.groups)
          ? parsed.groups
          : null
    if (!candidates) continue
    matched = true
    candidates.forEach((raw, index) => {
      const group = normalizeGroup(raw, groups.length + index)
      if (group) groups.push(group)
    })
  }
  if (!matched) return null
  return groups.length ? groups : null
}

/** 消息里是否含用例 JSON（用于决定是否展示 CaseResultCard）。 */
export function hasAgentCaseJson(content: string): boolean {
  return parseAgentCaseGroups(content) !== null
}

/** 移除消息里可解析为用例契约的 ```json 代码块（含流式中尚未闭合的尾部），仅用于展示；原始 content 仍用于转存。 */
export function stripAgentCaseJson(content: string): string {
  if (!content) return ""
  const pattern = /```json\s*([\s\S]*?)```/gi
  let result = ""
  let lastIndex = 0
  let match: RegExpExecArray | null
  while ((match = pattern.exec(content))) {
    const start = match.index
    const block = match[0]
    result += content.slice(lastIndex, start)
    if (!parseAgentCaseGroups(block)) result += block
    lastIndex = start + block.length
  }
  let tail = content.slice(lastIndex)
  const partial = tail.search(/```json\s*/i)
  if (partial !== -1) tail = tail.slice(0, partial)
  result += tail
  return result.trim()
}
