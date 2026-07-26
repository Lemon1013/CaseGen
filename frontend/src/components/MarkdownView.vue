<script setup lang="ts">
import { computed } from 'vue'
import MarkdownIt from 'markdown-it'

const props = withDefaults(
  defineProps<{
    content?: string | null
  }>(),
  {
    content: '',
  },
)

const emit = defineEmits<{
  /** Fired for in-app / relative links (browser navigation is prevented). */
  linkClick: [href: string]
}>()

const md = new MarkdownIt({
  html: false,
  linkify: true,
  breaks: true,
})

const html = computed(() => md.render(props.content || ''))

function isExternalHref(href: string) {
  return /^(https?:|mailto:|tel:)/i.test(href)
}

function onClick(e: MouseEvent) {
  const el = e.target
  if (!(el instanceof Element)) return
  const a = el.closest('a')
  if (!a) return
  const href = a.getAttribute('href')
  if (!href || href === '#') return

  // In-document hash anchors (not Vue history hashes like #/path)
  if (href.startsWith('#') && !href.startsWith('#/')) return

  if (isExternalHref(href)) {
    // Keep external links usable; open in new tab when possible
    if (!a.getAttribute('target')) {
      a.setAttribute('target', '_blank')
      a.setAttribute('rel', 'noopener noreferrer')
    }
    return
  }

  // Relative / SPA paths like pages/foo.md or /pages/foo.md — never full-page navigate
  e.preventDefault()
  e.stopPropagation()
  emit('linkClick', href)
}
</script>

<template>
  <div class="markdown-view" v-html="html" @click="onClick" />
</template>

<style scoped>
.markdown-view {
  line-height: 1.7;
  color: var(--cg-text);
  word-break: break-word;
}

.markdown-view :deep(h1),
.markdown-view :deep(h2),
.markdown-view :deep(h3),
.markdown-view :deep(h4) {
  margin: 1em 0 0.5em;
  font-weight: 600;
  line-height: 1.3;
}

.markdown-view :deep(h1) {
  font-size: 1.5em;
}

.markdown-view :deep(h2) {
  font-size: 1.3em;
}

.markdown-view :deep(h3) {
  font-size: 1.15em;
}

.markdown-view :deep(p) {
  margin: 0.6em 0;
}

.markdown-view :deep(ul),
.markdown-view :deep(ol) {
  padding-left: 1.4em;
  margin: 0.6em 0;
}

.markdown-view :deep(code) {
  background: #eef2ff;
  padding: 0.1em 0.35em;
  border-radius: 4px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.92em;
  color: #4338ca;
}

.markdown-view :deep(pre) {
  background: #0b1220;
  color: #e5e5e5;
  padding: 12px 14px;
  border-radius: 10px;
  overflow: auto;
  border: 1px solid rgba(255, 255, 255, 0.06);
}

.markdown-view :deep(pre code) {
  background: transparent;
  padding: 0;
  color: inherit;
}

.markdown-view :deep(blockquote) {
  margin: 0.8em 0;
  padding: 0.2em 0.8em;
  border-left: 4px solid var(--cg-primary);
  color: var(--cg-text-secondary);
  background: rgba(79, 124, 255, 0.05);
}

.markdown-view :deep(table) {
  border-collapse: collapse;
  width: 100%;
  margin: 0.8em 0;
}

.markdown-view :deep(th),
.markdown-view :deep(td) {
  border: 1px solid #ebeef5;
  padding: 6px 10px;
  text-align: left;
}

.markdown-view :deep(th) {
  background: #f5f7ff;
}

.markdown-view :deep(a) {
  color: var(--cg-primary);
  cursor: pointer;
}
</style>
