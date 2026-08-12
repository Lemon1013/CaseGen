export class ApiError extends Error {
  status: number
  detail: string

  constructor(status: number, detail: string) {
    super(detail)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

type UnauthorizedHandler = (requestEpoch: number) => void | Promise<void>
let unauthorizedHandler: UnauthorizedHandler | null = null
let authEpoch = 0

export function setUnauthorizedHandler(handler: UnauthorizedHandler | null) {
  unauthorizedHandler = handler
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

function isUnsafe(method?: string) {
  return !['GET', 'HEAD', 'OPTIONS', 'TRACE'].includes((method || 'GET').toUpperCase())
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const requestEpoch = authEpoch
  const headers = new Headers(init?.headers)
  if (init?.body && !(init.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json")
  }
  if (isUnsafe(init?.method)) {
    const csrf = readCookie('casegen_csrf')
    if (csrf && !headers.has('X-CSRF-Token')) headers.set('X-CSRF-Token', csrf)
  }
  const res = await fetch(path, { ...init, headers, credentials: 'include' })
  if (!res.ok) {
    const text = await res.text()
    let detail = text
    try {
      const payload = JSON.parse(text)
      if (typeof payload?.detail === 'string') detail = payload.detail
      else if (Array.isArray(payload?.detail)) {
        detail = payload.detail.map((item: { msg?: string }) => item.msg || '').filter(Boolean).join('；')
      }
    } catch {
      // Non-JSON gateway errors fall through to the status-aware message.
    }
    const translated = translateApiError(detail, res.status)
    if (res.status === 401 && !path.startsWith('/api/auth/')) {
      void unauthorizedHandler?.(requestEpoch)
    }
    throw new ApiError(res.status, translated)
  }
  if (res.status === 204) return undefined as T
  return res.json()
}

export async function apiBlob(path: string, init?: RequestInit): Promise<Blob> {
  const requestEpoch = authEpoch
  const headers = new Headers(init?.headers)
  if (isUnsafe(init?.method)) {
    const csrf = readCookie('casegen_csrf')
    if (csrf && !headers.has('X-CSRF-Token')) headers.set('X-CSRF-Token', csrf)
  }
  const res = await fetch(path, { ...init, headers, credentials: 'include' })
  if (!res.ok) {
    const text = await res.text()
    if (res.status === 401 && !path.startsWith('/api/auth/')) {
      void unauthorizedHandler?.(requestEpoch)
    }
    throw new ApiError(res.status, text || `请求失败（HTTP ${res.status}）`)
  }
  return res.blob()
}

function translateApiError(detail: string, status: number) {
  const rules: Array<[RegExp, string]> = [
    [/Unsupported file extension/i, '不支持该文件格式，请上传 md、txt、pdf 或 docx 文件'],
    [/File exceeds max size/i, '文件超过上传大小限制，请压缩或拆分后重试'],
    [/Parse failed|Could not parse|Document path is outside/i, '文档解析失败，请检查文件是否损坏或包含可提取文本'],
    [/Only pending review items/i, '该审核项已被处理，请刷新列表后查看最新状态'],
    [/Merge candidates require manual review/i, '合并候选暂不能自动批准，请人工核对冲突内容'],
    [/Ingest job not found/i, '摄入任务不存在或已被清理，请刷新文档列表'],
    [/Document not found/i, '文档不存在，请刷新列表后重试'],
    [/Wiki page not found/i, 'Wiki 页面不存在，可能已被归档或删除'],
    [/Source chunk not found/i, '原文块不存在，请重新摄入对应文档'],
  ]
  for (const [pattern, message] of rules) {
    if (pattern.test(detail)) return message
  }
  if (detail && !detail.startsWith('{') && !detail.startsWith('<')) return detail
  const fallback: Record<number, string> = {
    400: '请求参数不正确，请检查输入后重试',
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
