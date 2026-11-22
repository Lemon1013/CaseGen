export async function copyText(text: string): Promise<boolean> {
  if (!text) return false

  try {
    if (typeof navigator !== 'undefined' && navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch {
    // 浏览器权限受限或非安全上下文时回退 execCommand
  }

  if (typeof document === 'undefined' || !document.body) {
    return false
  }

  let textarea: HTMLTextAreaElement | null = null
  try {
    textarea = document.createElement('textarea')
    textarea.value = text
    textarea.setAttribute('readonly', '')
    textarea.style.position = 'fixed'
    textarea.style.top = '-9999px'
    textarea.style.left = '-9999px'
    textarea.style.opacity = '0'
    document.body.appendChild(textarea)
    textarea.focus({ preventScroll: true })
    textarea.select()
    return Boolean(document.execCommand('copy'))
  } catch {
    return false
  } finally {
    if (textarea && textarea.parentNode) {
      textarea.parentNode.removeChild(textarea)
    }
  }
}
