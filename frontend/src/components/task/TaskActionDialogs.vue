<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import type { FinalizeConflict, PromptRevision } from '../../api/tasks'
import type { ModelConfig } from '../../api/models'
import { caseStatusLabel, caseStatusTagType } from '../../utils/caseDictionaries'

export interface ApplyPromptPayload {
  content: string
  alsoRegenerate: boolean
}

const props = defineProps<{
  applyVisible: boolean
  selectedRevision: PromptRevision | null
  retryModelVisible: boolean
  models: ModelConfig[]
  defaultModelId: number | null
  taskErrorMessage?: string | null
  finalizeConflictVisible: boolean
  finalizeConflicts: FinalizeConflict[]
  acting: boolean
}>()

const emit = defineEmits<{
  (e: 'update:applyVisible', value: boolean): void
  (e: 'confirm-apply', payload: ApplyPromptPayload): void
  (e: 'update:retryModelVisible', value: boolean): void
  (e: 'confirm-retry-model', modelId: number): void
  (e: 'update:finalizeConflictVisible', value: boolean): void
  (e: 'confirm-finalize-overwrite'): void
}>()

const editedPromptContent = ref('')
const alsoRegenerate = ref(true)
const selectedModelId = ref<number | null>(null)

watch(
  () => [props.applyVisible, props.selectedRevision?.id] as const,
  ([visible]) => {
    if (visible && props.selectedRevision) {
      editedPromptContent.value = props.selectedRevision.new_content
      alsoRegenerate.value = true
    }
  },
)

watch(
  () => [props.retryModelVisible, props.defaultModelId, props.models.length] as const,
  ([visible]) => {
    if (visible) {
      selectedModelId.value =
        props.defaultModelId
        ?? props.models.find((model) => model.is_default)?.id
        ?? props.models[0]?.id
        ?? null
    }
  },
)

const applyVisibleModel = computed({
  get: () => props.applyVisible,
  set: (value: boolean) => emit('update:applyVisible', value),
})

const retryModelVisibleModel = computed({
  get: () => props.retryModelVisible,
  set: (value: boolean) => emit('update:retryModelVisible', value),
})

const finalizeConflictVisibleModel = computed({
  get: () => props.finalizeConflictVisible,
  set: (value: boolean) => emit('update:finalizeConflictVisible', value),
})

function handleConfirmApply() {
  if (!editedPromptContent.value.trim()) {
    ElMessage.warning('Prompt 内容不能为空')
    return
  }
  emit('confirm-apply', {
    content: editedPromptContent.value,
    alsoRegenerate: alsoRegenerate.value,
  })
}

function handleConfirmRetryModel() {
  if (selectedModelId.value == null) {
    ElMessage.warning('请先选择重试模型')
    return
  }
  emit('confirm-retry-model', selectedModelId.value)
}
</script>

<template>
  <div>
    <el-dialog
      v-model="applyVisibleModel"
      title="应用优化后的 Prompt"
      width="640px"
      destroy-on-close
    >
      <div v-if="selectedRevision" class="dialog-body">
        <div class="dialog-meta">修订 #{{ selectedRevision.id }} · {{ selectedRevision.status }}</div>
        <el-input v-model="editedPromptContent" type="textarea" :rows="12" />
        <div class="dialog-hint">可在确认前手动修改；最终保存的是此处内容。</div>
        <el-alert
          type="info"
          show-icon
          :closable="false"
          title="此修订仅对当前任务临时生效，不会影响系统或其他用户的提示词模板"
        />
        <el-checkbox v-model="alsoRegenerate">应用后立即再生成</el-checkbox>
      </div>
      <template #footer>
        <el-button @click="applyVisibleModel = false">取消</el-button>
        <el-button type="primary" :loading="acting" @click="handleConfirmApply">确认应用</el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="retryModelVisibleModel"
      title="切换模型并重新生成"
      width="500px"
      destroy-on-close
    >
      <div class="dialog-body">
        <el-alert
          v-if="taskErrorMessage"
          type="error"
          show-icon
          :closable="false"
          :title="taskErrorMessage"
        />
        <div class="dialog-meta">为当前失败任务选择新的推理模型，切换后会立即触发重新生成。</div>
        <el-select v-model="selectedModelId" placeholder="选择重试模型" style="width: 100%">
          <el-option
            v-for="model in models"
            :key="model.id"
            :label="`${model.name} · ${model.model_name}${model.is_default ? '（默认）' : ''}`"
            :value="model.id"
          />
        </el-select>
      </div>
      <template #footer>
        <el-button @click="retryModelVisibleModel = false">取消</el-button>
        <el-button
          type="warning"
          :loading="acting"
          :disabled="selectedModelId == null"
          @click="handleConfirmRetryModel"
        >
          切换并重新生成
        </el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="finalizeConflictVisibleModel"
      title="更新已有用例确认"
      width="680px"
      destroy-on-close
    >
      <div class="dialog-body">
        <el-alert
          type="warning"
          show-icon
          :closable="false"
          :title="`本次终版会更新 ${finalizeConflicts.length} 条该需求下已存在的用例`"
          description="更新会按 case_key 覆盖已有用例内容并记录操作日志，确认后不可撤销。"
        />
        <el-table :data="finalizeConflicts" stripe size="small" max-height="320">
          <el-table-column prop="case_key" label="用例编号" width="120" />
          <el-table-column prop="existing_title" label="已有标题" min-width="170" show-overflow-tooltip />
          <el-table-column label="已有状态" width="90">
            <template #default="{ row }">
              <el-tag size="small" :type="caseStatusTagType(row.existing_status)">
                {{ caseStatusLabel(row.existing_status) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="new_title" label="新草稿标题" min-width="170" show-overflow-tooltip />
        </el-table>
      </div>
      <template #footer>
        <el-button @click="finalizeConflictVisibleModel = false">返回</el-button>
        <el-button type="warning" :loading="acting" @click="emit('confirm-finalize-overwrite')">
          继续终版并覆盖
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.dialog-body {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.dialog-meta,
.dialog-hint {
  color: var(--cg-text-muted);
  font-size: 12px;
}
</style>
