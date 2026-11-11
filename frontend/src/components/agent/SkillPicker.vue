<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { MagicStick, Refresh } from '@element-plus/icons-vue'
import type { AgentSkillItem } from '../../api/agent'

// 技能 + 模型选择器。
// 技能来自 GET /api/agent/skills（后端扫描 .agents 下每个技能目录的 SKILL.md frontmatter，spec §3.4）；
// 模型支持从 ModelConfig 候选中下拉选择，也支持直接自由输入 provider/model 字符串（filterable + allow-create）。

const props = withDefaults(
  defineProps<{
    skills?: AgentSkillItem[]
    models?: { id: number; name: string; model_name: string }[]
    modelValue?: string | null
    modelLoading?: boolean
    disabled?: boolean
    /** true 时展示技能说明的折叠面板；false 只留一行描述。 */
    showDetail?: boolean
  }>(),
  {
    skills: () => [],
    models: () => [],
    modelValue: '',
    modelLoading: false,
    disabled: false,
    showDetail: true,
  },
)

const emit = defineEmits<{
  'update:modelValue': [value: string]
  'skillChange': [skillName: string | null]
  'refresh': []
}>()

const selectedSkill = ref<string | null>(null)
const modelDraft = ref<string>(props.modelValue || '')
const expanded = ref(false)

watch(
  () => props.modelValue,
  (value) => {
    if ((value || '') !== modelDraft.value) modelDraft.value = value || ''
  },
)

watch(
  () => props.skills,
  (list) => {
    // 列表刷新后保持已选技能；若技能被移除则清空选择。
    if (!selectedSkill.value) return
    if (!list.some((item) => item.name === selectedSkill.value)) {
      selectedSkill.value = null
      emit('skillChange', null)
    }
  },
  { deep: true },
)

const skillOptions = computed(() =>
  props.skills.map((item) => ({
    name: item.name,
    description: item.description || '（该技能未填写 description）',
  })),
)

const modelOptions = computed(() =>
  props.models.map((item) => ({
    label: `${item.name}（${item.model_name}）`,
    value: item.name ? `${item.name}/${item.model_name}` : item.model_name,
  })),
)

const currentSkill = computed(
  () => skillOptions.value.find((item) => item.name === selectedSkill.value) ?? null,
)

function onSkillChange(value: string | null) {
  selectedSkill.value = value || null
  emit('skillChange', selectedSkill.value)
  if (selectedSkill.value) {
    ElMessage.success(`已选择技能 ${selectedSkill.value}`)
  }
}

function onModelChange(value: unknown) {
  const next = value == null ? '' : String(value)
  modelDraft.value = next
  emit('update:modelValue', next)
}

function refresh() {
  emit('refresh')
}
</script>

<template>
  <div class="skill-picker" :class="{ 'is-disabled': disabled }">
    <div class="picker-row">
      <div class="picker-field skill-field">
        <div class="picker-label-row">
          <label class="picker-label">
            <el-icon><MagicStick /></el-icon>
            <span>专家技能</span>
          </label>
          <el-button
            size="small"
            link
            type="primary"
            :loading="modelLoading"
            :disabled="disabled"
            @click="refresh"
          >
            <el-icon><Refresh /></el-icon>
            <span>刷新</span>
          </el-button>
        </div>
        <el-select
          :model-value="selectedSkill"
          placeholder="不指定技能（通用对话）"
          clearable
          fit-input-width
          popper-class="skill-select-popper"
          :disabled="disabled"
          :loading="modelLoading"
          class="picker-select"
          @update:model-value="onSkillChange"
        >
          <el-option
            v-for="item in skillOptions"
            :key="item.name"
            :label="item.name"
            :value="item.name"
          >
            <div class="skill-option">
              <span class="skill-option-name">{{ item.name }}</span>
              <span class="skill-option-desc">{{ item.description }}</span>
            </div>
          </el-option>
        </el-select>
      </div>
      <div class="picker-field model-field">
        <div class="picker-label-row">
          <label class="picker-label">模型（可选或自定义输入）</label>
        </div>
        <el-select
          :model-value="modelDraft"
          placeholder="选择已配置模型或输入 provider/model，留空用默认"
          clearable
          filterable
          allow-create
          default-first-option
          :disabled="disabled"
          class="picker-select"
          @update:model-value="onModelChange"
        >
          <el-option
            v-for="item in modelOptions"
            :key="item.value"
            :label="item.label"
            :value="item.value"
          />
        </el-select>
      </div>
    </div>
    <div v-if="showDetail && currentSkill" class="picker-detail">
      <el-button size="small" link type="primary" @click="expanded = !expanded">
        {{ expanded ? '收起技能说明' : '查看技能说明' }}
      </el-button>
      <p v-if="expanded" class="detail-text">{{ currentSkill.description }}</p>
    </div>
    <p v-else-if="!skills.length && !modelLoading" class="picker-hint">
      暂未发现可用技能（后端需扫描 .agents/skills/*/SKILL.md）
    </p>
  </div>
</template>

<style scoped>
.skill-picker {
  display: flex;
  flex-direction: column;
  gap: 8px;
  width: 100%;
}

.skill-picker.is-disabled {
  opacity: 0.6;
}

.picker-row {
  display: flex;
  gap: 14px;
  flex-wrap: wrap;
}

.picker-field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 220px;
  flex: 1;
}

.picker-label-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  min-height: 20px;
}

.picker-label {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  font-weight: 600;
  color: var(--cg-text-secondary, #52525b);
}

.picker-select {
  width: 100%;
}

.skill-option {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 2px 0;
}

.skill-option-name {
  font-weight: 600;
  font-family: var(--cg-font-mono);
  font-size: 12.5px;
}

.skill-option-desc {
  font-size: 12px;
  color: var(--cg-text-muted, #71717a);
  line-height: 1.4;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

:global(.skill-select-popper) {
  max-width: min(420px, calc(100vw - 32px));
}

:global(.skill-select-popper .el-select-dropdown__item) {
  height: auto;
  min-height: 34px;
  line-height: 1.35;
  padding: 6px 32px 6px 20px;
  white-space: normal;
  box-sizing: border-box;
}

:global(.skill-select-popper .skill-option) {
  width: 100%;
  min-width: 0;
}

:global(.skill-select-popper .skill-option-desc) {
  max-width: 100%;
  min-width: 0;
}

.picker-detail {
  display: flex;
  flex-direction: column;
  gap: 4px;
  align-items: flex-start;
}

.detail-text {
  margin: 0;
  font-size: 12.5px;
  color: var(--cg-text-secondary, #52525b);
  line-height: 1.55;
  background: var(--cg-surface-muted, #f4f4f5);
  border-radius: var(--cg-radius-sm, 6px);
  padding: 8px 10px;
  width: 100%;
}

.picker-hint {
  margin: 0;
  font-size: 12px;
  color: var(--cg-text-muted, #71717a);
}
</style>
