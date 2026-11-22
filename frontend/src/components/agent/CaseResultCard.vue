<script setup lang="ts">
import StructuredCaseTree from '../common/StructuredCaseTree.vue'
import type { DraftCaseItem, DraftPointGroup } from '../../api/tasks'

/**
 * Agent 产出的用例结构化卡片（复用共享组件 StructuredCaseTree）。
 */
withDefaults(
  defineProps<{
    groups: DraftPointGroup[]
    citationTitles?: Record<number, string>
    defaultExpandedCount?: number | 'all'
  }>(),
  {
    citationTitles: undefined,
    defaultExpandedCount: 1,
  },
)

const emit = defineEmits<{
  (e: 'copy-case', item: DraftCaseItem): void
  (e: 'click-citation', citationId: number): void
}>()
</script>

<template>
  <div class="case-result-card">
    <StructuredCaseTree
      :groups="groups"
      :citation-titles="citationTitles"
      :default-expanded-count="defaultExpandedCount"
      :hide-empty-dimension="true"
      empty-text="未从回复中解析到结构化用例，建议查看原文或导出 Markdown"
      @copy-case="emit('copy-case', $event)"
      @click-citation="emit('click-citation', $event)"
    />
  </div>
</template>

<style scoped>
.case-result-card {
  width: 100%;
}
</style>
