<script setup lang="ts">
import { computed } from 'vue'
import { Aim, Document, Loading } from '@element-plus/icons-vue'
import MarkdownView from '../MarkdownView.vue'
import StructuredCaseTree from '../common/StructuredCaseTree.vue'
import type { CaseDraft, TaskCitation } from '../../api/tasks'
import { formatDateTime as formatTime } from '../../utils/datetime'
import { taskStatusLabel } from '../../utils/caseDictionaries'

const props = defineProps<{
  drafts: CaseDraft[]
  activeDraftTab: string
  draftViewMode: 'structured' | 'markdown'
  finalizedDraftId?: number | null
  citations: TaskCitation[]
  draftScrollStyle: Record<string, string>
  showLivePreview: boolean
  livePreviewText: string
  liveStageMessage: string
  liveStageStatus: string
}>()

const emit = defineEmits<{
  (e: 'update:activeDraftTab', value: string): void
  (e: 'update:draftViewMode', value: 'structured' | 'markdown'): void
}>()

const activeDraftTabModel = computed({
  get: () => props.activeDraftTab,
  set: (value: string) => emit('update:activeDraftTab', value),
})

const draftViewModeModel = computed({
  get: () => props.draftViewMode,
  set: (value: 'structured' | 'markdown') => emit('update:draftViewMode', value),
})

const liveProgressInfo = computed(() => {
  const match = /\[(\d+)\/(\d+)\]/.exec(props.liveStageMessage || '')
  if (match) {
    const current = parseInt(match[1], 10)
    const total = parseInt(match[2], 10)
    const percent = total > 0 ? Math.min(100, Math.round((current / total) * 100)) : 0
    return { current, total, percent, active: true }
  }
  return { current: 0, total: 0, percent: 0, active: false }
})
</script>

<template>
  <div class="draft-viewer-wrapper">
    <el-card v-if="showLivePreview" shadow="never" class="block live-preview-card">
      <template #header>
        <div class="card-head">
          <div class="head-left">
            <span class="preview-title">实时生成预览</span>
            <el-tag type="warning" size="small" effect="plain" class="live-tag">
              <el-icon class="is-loading"><Loading /></el-icon> 实时生成中
            </el-tag>
          </div>
          <span v-if="liveProgressInfo.active" class="progress-pill">
            {{ liveProgressInfo.current }} / {{ liveProgressInfo.total }} 测试点
          </span>
        </div>
      </template>

      <div v-if="liveProgressInfo.active" class="live-progress-container">
        <el-progress
          :percentage="liveProgressInfo.percent"
          :stroke-width="10"
          status="success"
          :striped="true"
          :striped-flow="true"
          class="live-progress-bar"
        />
      </div>

      <div v-if="liveStageMessage" class="live-preview-status">
        {{ liveStageStatus ? `${taskStatusLabel(liveStageStatus)} · ` : '' }}{{ liveStageMessage }}
      </div>

      <div class="live-preview-content">
        <MarkdownView v-if="livePreviewText" :content="livePreviewText" />
        <el-empty v-else description="正在等待模型输出…" :image-size="56" />
      </div>
    </el-card>

    <el-card shadow="never" class="block draft-container-card">
      <template #header>
        <div class="card-head draft-card-header">
          <div class="header-title-row">
            <span class="main-title">测试场景草稿</span>
            <span v-if="drafts.length" class="draft-count-tag">共 {{ drafts.length }} 个版本</span>
          </div>
          <el-radio-group v-model="draftViewModeModel" size="small" class="view-mode-toggle">
            <el-radio-button value="structured">
              <el-icon style="margin-right: 4px; vertical-align: -1px"><Aim /></el-icon>
              结构化场景矩阵
            </el-radio-button>
            <el-radio-button value="markdown">
              <el-icon style="margin-right: 4px; vertical-align: -1px"><Document /></el-icon>
              Markdown 全文
            </el-radio-button>
          </el-radio-group>
        </div>
      </template>

      <el-tabs v-if="drafts.length" v-model="activeDraftTabModel" class="draft-tabs">
        <el-tab-pane
          v-for="d in drafts"
          :key="d.id"
          :label="`v${d.version}`"
          :name="String(d.id)"
        >
          <div class="draft-meta">
            <span class="meta-time">{{ formatTime(d.created_at) }}</span>
            <span v-if="d.prompt_version_ref" class="meta-prompt"> · {{ d.prompt_version_ref }}</span>
            <el-tag
              v-if="finalizedDraftId === d.id"
              type="success"
              size="small"
              effect="plain"
              style="margin-left: 8px"
            >
              已定稿并导入
            </el-tag>
          </div>

          <div
            v-if="draftViewModeModel === 'structured'"
            class="draft-scroll structured-draft-view"
            :style="draftScrollStyle"
          >
            <StructuredCaseTree
              :groups="d.points_with_cases || []"
              :citations="citations"
              empty-text="暂无结构化测试点关联数据，建议切换至 Markdown 视图查看"
            />
          </div>

          <div v-else class="draft-scroll markdown-draft-view" :style="draftScrollStyle">
            <MarkdownView :content="d.content_md" />
          </div>
        </el-tab-pane>
      </el-tabs>

      <el-empty v-else description="暂无草稿" :image-size="64" />
    </el-card>
  </div>
</template>

<style scoped>
.block {
  margin-bottom: 16px;
  border-radius: var(--cg-radius) !important;
}

.block :deep(.el-card__header) {
  font-weight: 700;
  border-bottom-color: var(--cg-border);
}

.card-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.live-preview-card {
  border-color: rgba(230, 162, 60, 0.45) !important;
}

.live-progress-container {
  margin-bottom: 12px;
}

.head-left {
  display: flex;
  align-items: center;
  gap: 8px;
}

.progress-pill {
  font-size: 12px;
  font-weight: 600;
  background: #fef3c7;
  color: #b45309;
  padding: 2px 8px;
  border-radius: 10px;
}

.live-preview-status {
  margin-bottom: 14px;
  padding: 8px 10px;
  border-radius: var(--cg-radius-sm);
  color: var(--cg-text-secondary);
  font-size: 13px;
  background: var(--cg-surface-muted);
}

.draft-card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
}

.header-title-row {
  display: flex;
  align-items: center;
  gap: 10px;
}

.main-title {
  font-size: 16px;
  font-weight: 700;
  color: var(--cg-text-primary, #0f172a);
}

.draft-count-tag {
  font-size: 12px;
  color: var(--cg-text-secondary, #64748b);
  background: var(--cg-surface-muted, #f1f5f9);
  padding: 2px 8px;
  border-radius: 12px;
}

.view-mode-toggle :deep(.el-radio-button__inner) {
  font-weight: 500;
  padding: 6px 14px;
}

.draft-tabs {
  margin-top: -8px;
}

.draft-meta {
  display: flex;
  align-items: center;
  margin-bottom: 12px;
  font-size: 12px;
  color: var(--cg-text-secondary, #64748b);
}

.meta-time {
  font-family: ui-monospace, SFMono-Regular, monospace;
}

.draft-scroll {
  min-height: 0;
  overflow-y: auto;
  overflow-x: hidden;
  padding-right: 6px;
  scrollbar-width: thin;
  scrollbar-color: var(--cg-border-strong, #cbd5e1) transparent;
  box-sizing: border-box;
}

.draft-scroll::-webkit-scrollbar {
  width: 6px;
}

.draft-scroll::-webkit-scrollbar-track {
  background: transparent;
}

.draft-scroll::-webkit-scrollbar-thumb {
  background-color: var(--cg-border-strong, #cbd5e1);
  border-radius: 4px;
}

.draft-scroll::-webkit-scrollbar-thumb:hover {
  background-color: var(--cg-text-muted, #71717a);
}
</style>
