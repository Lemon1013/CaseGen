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
  background: #f4f4f5;
  border: 1px solid #e4e4e7;
  padding: 0.15em 0.4em;
  border-radius: 4px;
  font-family: var(--cg-font-mono);
  font-size: 0.9em;
  color: #18181b;
}

.markdown-view :deep(pre) {
  background: #09090b;
  color: #f4f4f5;
  padding: 12px 14px;
  border-radius: 8px;
  overflow: auto;
  border: 1px solid #18181b;
  font-family: var(--cg-font-mono);
}

.markdown-view :deep(pre code) {
  background: transparent;
  border: none;
  padding: 0;
  color: inherit;
}

.markdown-view :deep(blockquote) {
  margin: 0.8em 0;
  padding: 0.4em 0.9em;
  border-left: 3px solid #10b981;
  color: #52525b;
  background: #f8fafc;
}

.markdown-view :deep(table) {
  border-collapse: collapse;
  width: 100%;
  margin: 0.8em 0;
}

.markdown-view :deep(th),
.markdown-view :deep(td) {
  border: 1px solid #e4e4e7;
  padding: 6px 10px;
  text-align: left;
}

.markdown-view :deep(th) {
  background: #f4f4f5;
  color: #18181b;
}

.markdown-view :deep(a) {
  color: #059669;
  cursor: pointer;
  text-decoration: underline;
  text-underline-offset: 2px;
}

.markdown-view :deep(a:hover) {
  color: #10b981;
}
</style>
