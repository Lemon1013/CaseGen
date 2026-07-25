<script setup lang="ts">
import { computed } from 'vue'
import type { ReviewResult } from '../api/tasks'

const props = defineProps<{
  review: ReviewResult | null
}>()

const score = computed(() => props.review?.score ?? props.review?.payload?.score ?? null)
const verdict = computed(() => props.review?.verdict || props.review?.payload?.verdict || '-')
const issues = computed(() => props.review?.payload?.issues || [])
const missing = computed(() => props.review?.payload?.missing_scenarios || [])
const hints = computed(() => props.review?.payload?.prompt_improvement_hints || [])
const ready = computed(() => Boolean(props.review?.payload?.ready_for_final))
const highScore = computed(() => score.value != null && score.value >= 80)
</script>

<template>
  <div class="review-card" :class="{ highlight: ready || highScore }">
    <div class="header">
      <span class="title">评审结果</span>
      <div class="badges">
        <el-tag v-if="ready" type="success" effect="dark">ready_for_final</el-tag>
        <el-tag v-else-if="highScore" type="success">score ≥ 80</el-tag>
      </div>
    </div>

    <el-empty v-if="!review" description="尚未评审" :image-size="56" />

    <template v-else>
      <div class="score-row">
        <div class="score" :class="{ good: highScore }">{{ score ?? '-' }}</div>
        <div class="verdict">
          <div class="label">结论</div>
          <div class="value">{{ verdict }}</div>
        </div>
      </div>

      <div v-if="issues.length" class="section">
        <div class="section-title">问题</div>
        <ul>
          <li v-for="(item, i) in issues" :key="`i-${i}`">{{ item }}</li>
        </ul>
      </div>

      <div v-if="missing.length" class="section">
        <div class="section-title">缺失场景</div>
        <ul>
          <li v-for="(item, i) in missing" :key="`m-${i}`">{{ item }}</li>
        </ul>
      </div>

      <div v-if="hints.length" class="section">
        <div class="section-title">Prompt 改进建议</div>
        <ul>
          <li v-for="(item, i) in hints" :key="`h-${i}`">{{ item }}</li>
        </ul>
      </div>
    </template>
  </div>
</template>

<style scoped>
.review-card {
  border: 1px solid var(--cg-border);
  border-radius: var(--cg-radius);
  padding: 14px;
  background: #fff;
}

.review-card.highlight {
  border-color: rgba(18, 184, 134, 0.45);
  box-shadow: 0 0 0 1px rgba(18, 184, 134, 0.15);
  background: linear-gradient(180deg, rgba(18, 184, 134, 0.08) 0%, #fff 48%);
}

.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
}

.title {
  font-weight: 600;
}

.badges {
  display: flex;
  gap: 6px;
}

.score-row {
  display: flex;
  gap: 16px;
  align-items: center;
  margin-bottom: 12px;
}

.score {
  width: 76px;
  height: 76px;
  border-radius: 16px;
  background: linear-gradient(135deg, #eef2ff, #f5f3ff);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 28px;
  font-weight: 800;
  color: var(--cg-text-secondary);
  border: 1px solid var(--cg-border);
}

.score.good {
  background: linear-gradient(135deg, rgba(18, 184, 134, 0.16), rgba(79, 124, 255, 0.1));
  color: var(--cg-accent);
  border-color: rgba(18, 184, 134, 0.3);
}

.verdict .label {
  font-size: 12px;
  color: var(--cg-text-muted);
}

.verdict .value {
  font-size: 16px;
  font-weight: 600;
  margin-top: 4px;
}

.section {
  margin-top: 10px;
}

.section-title {
  font-size: 13px;
  font-weight: 600;
  margin-bottom: 4px;
}

.section ul {
  margin: 0;
  padding-left: 1.2em;
  color: #606266;
  font-size: 13px;
}

.section li {
  margin: 2px 0;
}
</style>
