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

const md = new MarkdownIt({
  html: false,
  linkify: true,
  breaks: true,
})

const html = computed(() => md.render(props.content || ''))
</script>

<template>
  <div class="markdown-view" v-html="html" />
</template>

<style scoped>
.markdown-view {
  line-height: 1.65;
  color: #303133;
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
  padding: 0.1em 0.35em;
  border-radius: 4px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.92em;
}

.markdown-view :deep(pre) {
  background: #1e1e1e;
  color: #e5e5e5;
  padding: 12px 14px;
  border-radius: 8px;
  overflow: auto;
}

.markdown-view :deep(pre code) {
  background: transparent;
  padding: 0;
  color: inherit;
}

.markdown-view :deep(blockquote) {
  margin: 0.8em 0;
  padding: 0.2em 0.8em;
  border-left: 4px solid #dcdfe6;
  color: #606266;
  background: #fafafa;
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
  background: #f5f7fa;
}

.markdown-view :deep(a) {
  color: #409eff;
}
</style>
