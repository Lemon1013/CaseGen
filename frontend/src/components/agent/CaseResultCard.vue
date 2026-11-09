<script setup lang="ts">
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Aim, CopyDocument, Link, Warning } from '@element-plus/icons-vue'
import type { DraftPointGroup } from '../../api/tasks'

/**
 * Agent 产出的用例结构化卡片。
 * 渲染 markup 搬自 TaskDetailView.vue:1539-1707（el-collapse 测试点树 + 用例卡片 +
 * copyCaseMarkdown(raw_md)），样式沿用同名 --cg-* 变量，保持与草稿确认页一致的观感。
 */

const props = defineProps<{
  groups: DraftPointGroup[]
  /** 引用 id → 标题，用于「关联证据」chip；agent 侧通常没有溯源数据。 */
  citationTitles?: Record<number, string>
  /** 默认展开的测试点数量（长对话里默认只展开首个，减少 DOM）。 */
  defaultExpandedCount?: number
}>()

const activePointNames = ref<string[]>([])

const groups = computed(() => props.groups ?? [])

function expandedDefaults(): string[] {
  const limit = Math.max(0, props.defaultExpandedCount ?? 1)
  return groups.value.slice(0, limit).map((group) => group.stable_key)
}

if (!activePointNames.value.length) activePointNames.value = expandedDefaults()

function getCitationTitle(id: number): string {
  return props.citationTitles?.[id] || `引用 #${id}`
}

function dimensionTagType(dimension?: string): 'primary' | 'success' | 'warning' | 'danger' | 'info' {
  const value = (dimension || '').toLowerCase()
  if (value.includes('positive') || value.includes('正向') || value.includes('主流程')) return 'primary'
  if (value.includes('boundary') || value.includes('边界')) return 'warning'
  if (value.includes('negative') || value.includes('异常') || value.includes('容错')) return 'danger'
  if (value.includes('permission') || value.includes('auth') || value.includes('权限')) return 'info'
  return 'info'
}

function priorityTagType(priority?: string): 'danger' | 'warning' | 'primary' | 'info' {
  const value = (priority || '').toUpperCase()
  if (value === 'P0') return 'danger'
  if (value === 'P1') return 'warning'
  if (value === 'P2') return 'info'
  return 'info'
}

/** 与 TaskDetailView.copyCaseMarkdown(:135-150) 同范式：clipboard API 失败回退 execCommand。 */
async function copyCaseMarkdown(rawMd: string, fallback: string) {
  const payload = rawMd || fallback
  if (!payload) {
    ElMessage.warning('该用例没有可复制的 Markdown 内容')
    return
  }
  try {
    await navigator.clipboard.writeText(payload)
    ElMessage.success('用例 Markdown 已成功复制到剪贴板')
  } catch {
    const textarea = document.createElement('textarea')
    textarea.value = payload
    textarea.style.position = 'fixed'
    textarea.style.opacity = '0'
    document.body.appendChild(textarea)
    textarea.select()
    document.execCommand('copy')
    document.body.removeChild(textarea)
    ElMessage.success('用例 Markdown 已成功复制到剪贴板')
  }
}

function caseFallbackMarkdown(caseKey: string, title: string): string {
  return `## ${caseKey} ${title}`.trim()
}
</script>

<template>
  <div class="case-result-card">
    <el-collapse v-model="activePointNames" class="point-collapse-tree">
      <el-collapse-item
        v-for="group in groups"
        :key="group.stable_key"
        :name="group.stable_key"
        class="point-collapse-item"
      >
        <template #title>
          <div class="point-header-bar">
            <div class="point-header-left">
              <el-tag
                v-if="group.dimension"
                size="small"
                :type="dimensionTagType(group.dimension)"
                effect="light"
                class="dimension-tag"
              >
                {{ group.dimension }}
              </el-tag>
              <el-tag :type="priorityTagType(group.priority)" effect="dark" class="priority-pill">
                {{ group.priority || 'P2' }}
              </el-tag>
              <span class="point-stable-key">{{ group.stable_key }}</span>
              <span class="point-title-text">{{ group.title }}</span>
            </div>
            <div class="point-header-right">
              <el-tag
                round
                :type="group.cases.length ? 'primary' : 'info'"
                effect="plain"
                class="case-count-pill"
              >
                {{ group.cases.length }} 条用例
              </el-tag>
            </div>
          </div>
        </template>
        <div class="point-body-content">
          <div class="point-goal-callout">
            <el-icon class="goal-icon"><Aim /></el-icon>
            <div class="goal-text">
              <span class="goal-label">AIM // 验证目标：</span>
              {{ group.verification_goal || '针对该测试点开展验证' }}
            </div>
          </div>
          <div v-if="group.citation_ids?.length" class="point-citations-row">
            <span class="citations-label">
              <el-icon><Link /></el-icon>
              关联证据：
            </span>
            <el-tag
              v-for="cid in group.citation_ids"
              :key="cid"
              size="small"
              type="info"
              class="citation-chip"
            >
              #{{ cid }} {{ getCitationTitle(cid) }}
            </el-tag>
          </div>
          <div v-if="group.cases.length" class="cases-tree-list">
            <div v-for="item in group.cases" :key="item.case_key" class="case-card-unit">
              <div class="case-card-header">
                <div class="case-title-left">
                  <span class="case-badge-key">{{ item.case_key }}</span>
                  <span class="case-main-title">{{ item.title }}</span>
                  <el-tag v-if="item.type" size="small" type="primary" effect="plain" class="case-type-tag">
                    {{ item.type }}
                  </el-tag>
                  <el-tag :type="priorityTagType(item.priority)" effect="light" class="case-priority-tag">
                    {{ item.priority || 'P2' }}
                  </el-tag>
                </div>
                <div class="case-actions-right">
                  <el-button
                    size="small"
                    link
                    type="primary"
                    class="copy-btn"
                    @click="copyCaseMarkdown(item.raw_md, caseFallbackMarkdown(item.case_key, item.title))"
                  >
                    <el-icon><CopyDocument /></el-icon>
                    <span>复制用例</span>
                  </el-button>
                </div>
              </div>
              <div class="case-card-body">
                <div v-if="item.preconditions" class="case-section-block">
                  <div class="section-label">前置条件</div>
                  <div class="section-text pre-box">{{ item.preconditions }}</div>
                </div>
                <div v-if="item.test_data" class="case-section-block">
                  <div class="section-label">测试数据</div>
                  <div class="section-text data-box">{{ item.test_data }}</div>
                </div>
                <div v-if="item.steps?.length" class="case-steps-flow">
                  <div class="section-label">测试步骤与断言</div>
                  <div class="steps-table-list">
                    <div v-for="step in item.steps" :key="step.step_no" class="step-item-row">
                      <div class="step-num-col">
                        <span class="step-pill">{{ step.step_no }}</span>
                      </div>
                      <div class="step-content-col">
                        <div class="step-action-desc">{{ step.action }}</div>
                        <div v-if="step.expected" class="step-expected-card">
                          <span class="expected-check-icon">✓</span>
                          <span class="expected-text-body">{{ step.expected }}</span>
                        </div>
                      </div>
                    </div>
                  </div>
                </div>
                <div v-if="item.pending_items?.length" class="case-pending-block">
                  <div class="pending-head">
                    <el-icon><Warning /></el-icon>
                    待定事项
                  </div>
                  <ul class="pending-list">
                    <li v-for="(pending, index) in item.pending_items" :key="index">{{ pending }}</li>
                  </ul>
                </div>
              </div>
            </div>
          </div>
          <el-empty
            v-else
            description="该测试点尚未生成衍生测试用例"
            :image-size="48"
            class="empty-cases-placeholder"
          />
        </div>
      </el-collapse-item>
    </el-collapse>
    <div v-if="!groups.length" class="structured-fallback">
      <el-empty description="未从回复中解析到结构化用例，建议查看原文或导出 Markdown" :image-size="64" />
    </div>
  </div>
</template>

<style scoped>
.case-result-card {
  width: 100%;
}

.point-collapse-tree {
  border: none;
  --el-collapse-header-height: auto;
}

.point-collapse-item {
  border: 1px solid var(--cg-border, #e2e8f0) !important;
  border-radius: 8px !important;
  margin-bottom: 12px;
  overflow: hidden;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.02);
  transition: all 0.2s ease-in-out;
}

.point-collapse-item:hover {
  border-color: #cbd5e1 !important;
  box-shadow: 0 3px 8px rgba(0, 0, 0, 0.04);
}

.point-collapse-item :deep(.el-collapse-item__header) {
  padding: 12px 16px;
  background: #f8fafc;
  border-bottom: 1px solid transparent;
  line-height: normal;
}

.point-collapse-item.is-active :deep(.el-collapse-item__header) {
  border-bottom-color: var(--cg-border, #e2e8f0);
}

.point-collapse-item :deep(.el-collapse-item__content) {
  padding: 16px;
  background: #ffffff;
}

.point-header-bar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  width: 100%;
  padding-right: 12px;
  gap: 12px;
}

.point-header-left {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

.dimension-tag {
  font-weight: 500;
  border-radius: 4px;
}

.priority-pill {
  font-family: var(--cg-font-mono);
  font-weight: 700;
  border-radius: 4px;
  padding: 0 6px;
}

.priority-pill.el-tag--danger {
  background: #ffe4e6 !important;
  color: #e11d48 !important;
  border: 1px solid #fecdd3 !important;
}

.priority-pill.el-tag--warning {
  background: #fef3c7 !important;
  color: #b45309 !important;
  border: 1px solid #fde68a !important;
}

.priority-pill.el-tag--info {
  background: #f4f4f5 !important;
  color: #52525b !important;
  border: 1px solid #e4e4e7 !important;
}

.point-stable-key {
  font-family: var(--cg-font-mono);
  font-weight: 700;
  font-size: 12px;
  color: #ffffff;
  background: #18181b;
  padding: 2px 8px;
  border-radius: 4px;
  letter-spacing: 0.5px;
}

.point-title-text {
  font-size: 14px;
  font-weight: 600;
  color: var(--cg-text, #0f172a);
}

.case-count-pill {
  font-weight: 600;
  font-size: 12px;
}

.point-goal-callout {
  display: flex;
  align-items: center;
  gap: 8px;
  background: #f0fdf4;
  border-left: 3px solid var(--cg-accent-brand, #10b981);
  border-radius: 6px;
  padding: 8px 12px;
  margin-bottom: 12px;
  font-size: 13px;
  color: #065f46;
}

.goal-icon {
  font-size: 16px;
  color: var(--cg-accent-brand, #10b981);
  flex-shrink: 0;
}

.goal-label {
  font-family: var(--cg-font-mono);
  font-weight: 700;
  color: #047857;
}

.point-citations-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 14px;
  font-size: 12px;
  color: var(--cg-text-secondary, #64748b);
}

.citations-label {
  display: flex;
  align-items: center;
  gap: 4px;
  font-weight: 600;
}

.citation-chip {
  font-size: 12px;
  font-family: var(--cg-font-mono);
  background: #f4f4f5 !important;
  border: 1px solid #e4e4e7 !important;
  color: #27272a !important;
}

.cases-tree-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.case-card-unit {
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  background: #ffffff;
  padding: 14px 16px;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.02);
  transition: all 0.2s ease;
}

.case-card-unit:hover {
  border-color: #cbd5e1;
  box-shadow: 0 4px 10px rgba(0, 0, 0, 0.04);
}

.case-card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  padding-bottom: 10px;
  border-bottom: 1px dashed #e2e8f0;
  margin-bottom: 12px;
}

.case-title-left {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

.case-badge-key {
  font-family: var(--cg-font-mono);
  font-weight: 700;
  font-size: 12px;
  color: #18181b;
  background: #f4f4f5;
  border: 1px solid #e4e4e7;
  padding: 2px 7px;
  border-radius: 4px;
}

.case-priority-tag.el-tag--danger {
  background: #ffe4e6 !important;
  color: #e11d48 !important;
  border: 1px solid #fecdd3 !important;
}

.case-priority-tag.el-tag--warning {
  background: #fef3c7 !important;
  color: #b45309 !important;
  border: 1px solid #fde68a !important;
}

.case-priority-tag.el-tag--info {
  background: #f4f4f5 !important;
  color: #52525b !important;
  border: 1px solid #e4e4e7 !important;
}

.case-main-title {
  font-size: 14px;
  font-weight: 600;
  color: #1e293b;
}

.copy-btn {
  font-size: 12px;
  font-weight: 500;
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 3px 8px;
  border: 1px solid #e4e4e7;
  border-radius: 4px;
  color: #52525b;
  background: #ffffff;
  transition: all 0.15s ease;
}

.copy-btn:hover {
  border-color: var(--cg-accent-brand, #10b981);
  color: var(--cg-accent-brand, #10b981);
  background: var(--cg-accent-brand-soft, #ecfdf5);
}

.case-card-body {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.case-section-block {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.section-label {
  font-size: 12px;
  font-weight: 700;
  color: #64748b;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.pre-box,
.data-box {
  background: #f8fafc;
  border: 1px solid #f1f5f9;
  border-radius: 6px;
  padding: 8px 12px;
  font-size: 13px;
  color: #334155;
  line-height: 1.5;
  white-space: pre-wrap;
}

.case-steps-flow {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.steps-table-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-top: 2px;
}

.step-item-row {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 6px 0;
}

.step-num-col {
  flex-shrink: 0;
  margin-top: 2px;
}

.step-pill {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  border-radius: 50%;
  background: #18181b;
  color: #ffffff;
  font-family: var(--cg-font-mono);
  font-size: 12px;
  font-weight: 700;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.12);
}

.step-content-col {
  flex: 1;
  min-width: 0;
}

.step-action-desc {
  font-size: 13px;
  line-height: 1.5;
  color: #1e293b;
  font-weight: 500;
}

.step-expected-card {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  background: #f0fdf4;
  border: 1px solid #bbf7d0;
  border-radius: 6px;
  padding: 6px 10px;
  margin-top: 6px;
  color: #166534;
  font-size: 13px;
  line-height: 1.45;
}

.expected-check-icon {
  font-weight: bold;
  color: var(--cg-accent-brand, #10b981);
  flex-shrink: 0;
}

.expected-text-body {
  word-break: break-word;
}

.case-pending-block {
  background: #fffbeb;
  border: 1px solid #fde68a;
  border-radius: 6px;
  padding: 8px 12px;
  color: #92400e;
  font-size: 12px;
}

.pending-head {
  display: flex;
  align-items: center;
  gap: 4px;
  font-weight: 700;
  margin-bottom: 4px;
}

.pending-list {
  margin: 0;
  padding-left: 18px;
}

.empty-cases-placeholder {
  padding: 4px 0;
}

.structured-fallback {
  padding: 4px 0;
}
</style>
