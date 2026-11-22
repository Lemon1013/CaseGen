export class ApiError extends Error {
  status: number
  detail: string
  rawDetail?: unknown

  constructor(status: number, detail: string, rawDetail?: unknown) {
    super(detail)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
    this.rawDetail = rawDetail
  }
}

type UnauthorizedHandler = (requestEpoch: number) => void | Promise<void>
type ProjectInvalidHandler = () => void | Promise<void>
let unauthorizedHandler: UnauthorizedHandler | null = null
let projectInvalidHandler: ProjectInvalidHandler | null = null
let authEpoch = 0

export function setUnauthorizedHandler(handler: UnauthorizedHandler | null) {
  unauthorizedHandler = handler
}

export function setProjectInvalidHandler(handler: ProjectInvalidHandler | null) {
  projectInvalidHandler = handler
}

export function getAuthEpoch() {
  return authEpoch
}

export function bumpAuthEpoch() {
  authEpoch += 1
  return authEpoch
}

function readCookie(name: string): string | null {
  if (typeof document === 'undefined') return null
  const prefix = `${encodeURIComponent(name)}=`
  const item = document.cookie.split('; ').find((entry) => entry.startsWith(prefix))
  return item ? decodeURIComponent(item.slice(prefix.length)) : null
}

const GLOBAL_API_PREFIXES = [
  '/api/health',
  '/api/auth',
  '/api/users',
  '/api/models',
  '/api/prompts',
  '/api/dashboard',
  '/api/knowledge-providers',
]

const GLOBAL_API_EXACT_PATHS = new Set(['/api/projects'])

function normalizePathname(pathname: string): string {
  if (pathname.length > 1 && pathname.endsWith('/')) {
    return pathname.slice(0, -1)
  }
  return pathname
}

export function isGlobalApiPath(pathname: string): boolean {
  const normalized = normalizePathname(pathname)
  if (GLOBAL_API_EXACT_PATHS.has(normalized)) return true
  return GLOBAL_API_PREFIXES.some(
    (prefix) => normalized === prefix || normalized.startsWith(`${prefix}/`),
  )
}

function isUnsafe(method?: string) {
  return !['GET', 'HEAD', 'OPTIONS', 'TRACE'].includes((method || 'GET').toUpperCase())
}

interface PreparedRequest {
  url: string
  headers: Headers
  requestEpoch: number
}

function prepareRequest(path: string, init?: RequestInit, jsonContentType = true): PreparedRequest {
  const requestEpoch = authEpoch
  const headers = new Headers(init?.headers)
  if (
    jsonContentType &&
    init?.body &&
    !(init.body instanceof FormData) &&
    !(init.body instanceof Blob) &&
    !headers.has('Content-Type')
  ) {
    headers.set('Content-Type', 'application/json')
  }
  if (isUnsafe(init?.method)) {
    const csrf = readCookie('casegen_csrf')
    if (csrf && !headers.has('X-CSRF-Token')) headers.set('X-CSRF-Token', csrf)
  }
  let resolvedPath = path
  if (typeof window !== 'undefined' && path.startsWith('/api/')) {
    const url = new URL(path, window.location.origin)
    if (!isGlobalApiPath(url.pathname) && !url.searchParams.has('project_id')) {
      const searchProjectId = new URLSearchParams(window.location.search).get('project_id')
      const projectId = searchProjectId || window.localStorage?.getItem('casegen:last-project-id')
      if (projectId) {
        url.searchParams.set('project_id', projectId)
        resolvedPath = `${url.pathname}${url.search}`
      }
    }
  }
  return { url: resolvedPath, headers, requestEpoch }
}

function extractErrorDetail(text: string): { detail: string; rawDetail: unknown } {
  let detail = text
  let rawDetail: unknown = text
  try {
    const payload = JSON.parse(text)
    if (payload && 'detail' in payload) {
      rawDetail = payload.detail
    }
    if (typeof payload?.detail === 'string') {
      detail = payload.detail
    } else if (Array.isArray(payload?.detail)) {
      detail = payload.detail
        .map((item: { msg?: string }) => item.msg || '')
        .filter(Boolean)
        .join('；')
    } else if (payload?.detail && typeof payload.detail === 'object') {
      const msg = (payload.detail as { message?: unknown }).message
      if (typeof msg === 'string' && msg) {
        detail = msg
      }
    }
  } catch {
    // Non-JSON gateway errors fall through to the status-aware message.
  }
  return { detail, rawDetail }
}

async function throwApiError(res: Response, path: string, requestEpoch: number): Promise<never> {
  const text = await res.text()
  const { detail, rawDetail } = extractErrorDetail(text)
  if (res.status === 404 && /Project not found or inactive/i.test(detail)) {
    void projectInvalidHandler?.()
  }
  const translated = translateApiError(detail, res.status)
  if (res.status === 401 && !path.startsWith('/api/auth/')) {
    void unauthorizedHandler?.(requestEpoch)
  }
  throw new ApiError(res.status, translated, rawDetail)
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const { url, headers, requestEpoch } = prepareRequest(path, init, true)
  const res = await fetch(url, { ...init, headers, credentials: 'include' })
  if (!res.ok) {
    await throwApiError(res, url, requestEpoch)
  }
  if (res.status === 204) return undefined as T
  return res.json()
}

export async function apiBlob(path: string, init?: RequestInit): Promise<Blob> {
  const { url, headers, requestEpoch } = prepareRequest(path, init, false)
  const res = await fetch(url, { ...init, headers, credentials: 'include' })
  if (!res.ok) {
    await throwApiError(res, url, requestEpoch)
  }
  return res.blob()
}

function translateApiError(detail: string, status: number) {
  const rules: Array<[RegExp, string]> = [
    [/Unsupported file extension/i, '不支持该文件格式，请上传 md、txt、pdf 或 docx 文件'],
    [/File exceeds max size/i, '文件超过上传大小限制，请压缩或拆分后重试'],
    [/Parse failed|Could not parse|Document path is outside/i, '文档解析失败，请检查文件是否损坏或包含可提取文本'],
    [/Another active prompt of this type is already enabled/i, '该类型已有启用中的提示词，请先停用其他模板后再启用这条'],
    [
      /Global prompt application is disabled|global mode is disabled|apply.*global.*disabled|only ['"]?task_temp['"]?.*allowed|已禁止修改全局提示词/i,
      '已禁止在任务内修改全局提示词，修订仅对当前任务临时生效',
    ],
    [
      /Only admin(?:istrator)?s? can set prompt visibility to system/i,
      '仅管理员可将提示词设为系统预置',
    ],
    [
      /Only admin(?:istrator)?s? can (?:manage|create|edit|update|modify|delete) (?:system )?(?:engine|non-generate) prompts?|Only admin(?:istrator)?s? can create system prompts|Admin (?:permission|privileges?) required for (?:system|engine) prompts?/i,
      '仅管理员可创建或管理系统预置及引擎类提示词',
    ],
    [
      /Cannot (?:edit|update|modify) (?:another user's|others'|system|this) prompt|Not authorized to (?:edit|update|modify) this prompt|No permission to (?:edit|modify) this prompt|You do not have permission to (?:edit|update|modify)/i,
      '无权编辑该提示词，请先「复制为我的模板」后再修改',
    ],
    [
      /Cannot delete (?:another user's|others'|system|this) prompt|Not authorized to delete this prompt|No permission to delete this prompt|You do not have permission to delete/i,
      '无权删除该提示词',
    ],
    [
      /Administrators cannot change another user's shared prompt to (?:private|system)|Cannot (?:change|set|downgrade) another user's (?:shared )?prompt to (?:private|system)|shared prompt.*cannot.*(?:private|system)/i,
      '不能将他人的团队共享模板修改为私有或系统预置',
    ],
    [
      /System engine prompts must (?:have|keep) visibility|Only generate prompts can be (?:shared|private|forked)|(?:Engine|Non-generate) prompts must (?:have visibility|be) ['"]?system['"]?/i,
      '引擎类提示词固定为系统预置，仅生成类（generate）提示词支持私有、共享或复制',
    ],
    [/Cannot delete the last active (?:system )?prompt/i, '这是该类型最后一个启用的提示词，请先启用同类型的其他模板'],
    [/Prompt is referenced by task/i, '该提示词已被任务引用，为保留任务可重现性不能删除'],
    [/Prompt is referenced by revision/i, '该提示词已被 Prompt 修订引用，为保留历史记录不能删除'],
    [/Prompt(?: template)? not found/i, '提示词不存在或无权访问，请刷新列表'],
  ]
  for (const [pattern, message] of rules) {
    if (pattern.test(detail)) return message
  }
  if (detail && !detail.startsWith('{') && !detail.startsWith('<')) return detail
  const fallback: Record<number, string> = {
    400: '请求参数不正确，请检查输入后重试',
    403: '没有权限执行该操作',
    404: '请求的数据不存在，请刷新页面',
    409: '数据状态已变化，请刷新后重试',
    413: '提交内容过大，请缩小文件或输入内容',
    422: '提交内容未通过校验，请检查必填项',
    500: '服务执行失败，请查看后台日志后重试',
    502: '模型服务暂时不可用，请稍后重试',
    503: '服务暂时不可用，请稍后重试',
  }
  return fallback[status] || `请求失败（HTTP ${status}），请稍后重试`
}
