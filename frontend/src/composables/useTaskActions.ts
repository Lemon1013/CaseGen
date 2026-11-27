import { ref, type ComputedRef, type Ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  applyPrompt,
  confirmRetrievalCheckpoint,
  confirmTestPoints,
  editTestPoints,
  finalizeTask,
  generateTask,
  getFinalizeConflicts,
  optimizePromptTask,
  regenerateTask,
  reviewTask,
  shouldPollTaskStatus,
  stopTask,
  updateTaskModel,
  type CaseDraft,
  type FinalizeConflict,
  type PromptRevision,
  type RetrievalCheckpoint,
  type TaskEvent,
  type TaskItem,
  type TestPointCheckpoint,
} from '../api/tasks'
import type { RetrievalConfirmPayload } from '../components/task/TaskRetrievalConfirmCard.vue'
import type { TestPointSubmissionItem } from '../components/task/TaskTestPointsEditorCard.vue'
import type { ApplyPromptPayload } from '../components/task/TaskActionDialogs.vue'

export interface UseTaskActionsOptions {
  taskId: ComputedRef<number>
  projectId: ComputedRef<number | null>
  task: Ref<TaskItem | null>
  drafts: Ref<CaseDraft[]>
  events: Ref<TaskEvent[]>
  revisions: Ref<PromptRevision[]>
  pendingRevisions: ComputedRef<PromptRevision[]>
  checkpoint: Ref<RetrievalCheckpoint | null>
  testPointCheckpoint: Ref<TestPointCheckpoint | null>
  activeDraftTab: Ref<string>
  applyTaskUpdate: (updated: TaskItem) => boolean
  applyTestPointCheckpointUpdate: (saved: TestPointCheckpoint) => boolean
  invalidatePendingDataReads: () => void
  refreshLight: (force?: boolean) => Promise<boolean>
}

export function useTaskActions(options: UseTaskActionsOptions) {
  const acting = ref(false)
  const confirmingCheckpoint = ref(false)
  const confirmingTestPoints = ref(false)
  const applyDialogVisible = ref(false)
  const selectedRevision = ref<PromptRevision | null>(null)
  const retryModelDialogVisible = ref(false)
  const finalizeConflictDialogVisible = ref(false)
  const finalizeConflicts = ref<FinalizeConflict[]>([])
  const pendingFinalizeDraftId = ref<number | null>(null)
  const pendingFinalizePendingCount = ref(0)

  function resetDialogState() {
    applyDialogVisible.value = false
    selectedRevision.value = null
    retryModelDialogVisible.value = false
    finalizeConflictDialogVisible.value = false
    finalizeConflicts.value = []
    pendingFinalizeDraftId.value = null
    pendingFinalizePendingCount.value = 0
  }

  function countPendingItemsCases(draftId: number | null): number {
    const targetDraft =
      (draftId != null ? options.drafts.value.find((item) => item.id === draftId) : null)
      || options.drafts.value[0]
      || null
    if (!targetDraft?.points_with_cases?.length) return 0
    const pendingKeys = new Set<string>()
    for (const group of targetDraft.points_with_cases) {
      const cases = group.cases || []
      cases.forEach((item, idx) => {
        if (item.pending_items?.length) {
          pendingKeys.add(item.case_key || `${group.stable_key}-${idx}`)
        }
      })
    }
    return pendingKeys.size
  }

  async function confirmCheckpoint(payload: RetrievalConfirmPayload) {
    const currentTask = options.task.value
    const currentCheckpoint = options.checkpoint.value
    if (!currentTask || !currentCheckpoint || confirmingCheckpoint.value) return
    confirmingCheckpoint.value = true
    try {
      const updated = await confirmRetrievalCheckpoint(currentTask.id, {
        selected_citation_ids: payload.selectedCitationIds,
        supplemental_text: payload.supplementalText,
        expected_version: currentCheckpoint.version,
        idempotency_key: `task-${currentTask.id}-checkpoint-${currentCheckpoint.id}-v${currentCheckpoint.version}`,
        allow_empty_context: payload.selectedCitationIds.length === 0,
      })
      if (!options.applyTaskUpdate(updated)) return
      await options.refreshLight(true)
    } catch (err) {
      if (currentTask.id === options.taskId.value) {
        ElMessage.error(`确认失败：${(err as Error).message}`)
      }
    } finally {
      confirmingCheckpoint.value = false
    }
  }

  async function saveTestPoints(points: TestPointSubmissionItem[]) {
    const currentCheckpoint = options.testPointCheckpoint.value
    if (!currentCheckpoint) return
    const targetTaskId = options.taskId.value
    confirmingTestPoints.value = true
    try {
      const saved = await editTestPoints(targetTaskId, {
        expected_version: currentCheckpoint.version,
        points,
      })
      if (saved.task_id !== options.taskId.value) return
      if (!options.applyTestPointCheckpointUpdate(saved)) return
      ElMessage.success('测试点编辑已保存')
    } catch (err) {
      if (targetTaskId === options.taskId.value) {
        ElMessage.error(`保存测试点失败：${(err as Error).message}`)
      }
    } finally {
      confirmingTestPoints.value = false
    }
  }

  async function confirmTestPointStage(points: TestPointSubmissionItem[]) {
    const currentTask = options.task.value
    const currentCheckpoint = options.testPointCheckpoint.value
    if (!currentTask || !currentCheckpoint) return
    confirmingTestPoints.value = true
    try {
      const updated = await confirmTestPoints(currentTask.id, {
        expected_version: currentCheckpoint.version,
        idempotency_key: `task-${currentTask.id}-test-points-${currentCheckpoint.id}-v${currentCheckpoint.version}`,
        points,
      })
      if (!options.applyTaskUpdate(updated)) return
      await options.refreshLight(true)
      ElMessage.success('测试点已确认，开始生成测试场景')
    } catch (err) {
      if (currentTask.id === options.taskId.value) {
        ElMessage.error(`确认测试点失败：${(err as Error).message}`)
      }
    } finally {
      confirmingTestPoints.value = false
    }
  }

  async function runAction(label: string, fn: (id: number) => Promise<TaskItem>) {
    const currentTask = options.task.value
    if (!currentTask) return
    acting.value = true
    try {
      ElMessage.info(`已提交${label}…`)
      const updated = await fn(currentTask.id)
      if (!options.applyTaskUpdate(updated)) return
      await options.refreshLight(true)
      if (updated.id !== options.taskId.value) return
      if (shouldPollTaskStatus(updated.status)) {
        ElMessage.success(`${label}已在后台执行，状态会自动刷新`)
      } else if (updated.status === 'failed') {
        ElMessage.error(`${label}失败：${updated.error_message || '未知错误'}`)
      } else {
        ElMessage.success(`${label}完成`)
      }
    } catch (e) {
      if (currentTask.id === options.taskId.value) {
        ElMessage.error(`${label}失败：${(e as Error).message}`)
        await options.refreshLight(true)
      }
    } finally {
      acting.value = false
    }
  }

  async function onStopGeneration() {
    const currentTask = options.task.value
    if (!currentTask || currentTask.status !== 'generating') return
    try {
      await ElMessageBox.confirm(
        '停止将中止进行中的生成，已产生的部分内容会被丢弃且不可恢复。确定停止吗？',
        '停止生成确认',
        { type: 'warning', confirmButtonText: '停止生成', cancelButtonText: '继续生成' },
      )
    } catch {
      return
    }
    acting.value = true
    try {
      const updated = await stopTask(currentTask.id, options.projectId.value)
      if (!options.applyTaskUpdate(updated)) return
      ElMessage.success('已停止生成')
      await options.refreshLight(true)
    } catch (e) {
      if (currentTask.id === options.taskId.value) {
        ElMessage.error(`停止生成失败：${(e as Error).message}`)
        await options.refreshLight(true)
      }
    } finally {
      acting.value = false
    }
  }

  async function onRegenerate() {
    try {
      await ElMessageBox.confirm('再生成将重新检索并覆盖当前生成流程，是否继续？', '再生成确认', {
        type: 'warning',
      })
    } catch {
      return
    }
    return runAction('再生成', regenerateTask)
  }

  async function onFinalize() {
    const currentTask = options.task.value
    if (!currentTask) return
    const selectedDraftId = options.activeDraftTab.value ? Number(options.activeDraftTab.value) : null
    let conflicts: FinalizeConflict[]
    acting.value = true
    try {
      conflicts = (await getFinalizeConflicts(currentTask.id, selectedDraftId)).conflicts
    } catch (e) {
      if (currentTask.id === options.taskId.value) {
        ElMessage.error(`检查终版冲突失败：${(e as Error).message}`)
      }
      return
    } finally {
      acting.value = false
    }
    if (currentTask.id !== options.taskId.value) return
    const pendingCount = countPendingItemsCases(selectedDraftId)
    if (conflicts.length > 0) {
      pendingFinalizeDraftId.value = selectedDraftId
      pendingFinalizePendingCount.value = pendingCount
      finalizeConflicts.value = conflicts
      finalizeConflictDialogVisible.value = true
      return
    }
    const confirmMessage = pendingCount > 0
      ? `当前草稿包含 ${pendingCount} 条含待确认项的场景，确认入库后将保留待确认标记。确认将当前草稿标记为终版并入库？`
      : '确认将当前草稿标记为终版并入库？'
    try {
      await ElMessageBox.confirm(confirmMessage, '终版确认', { type: 'warning' })
    } catch {
      return
    }
    if (currentTask.id !== options.taskId.value) return
    return runAction('终版', (id) => finalizeTask(id, selectedDraftId, false))
  }

  async function confirmFinalizeOverwrite() {
    finalizeConflictDialogVisible.value = false
    const draftId = pendingFinalizeDraftId.value
    await runAction('终版', (id) => finalizeTask(id, draftId, true))
  }

  async function onRetryFailed() {
    if (!options.task.value) return
    const step = [...options.events.value].reverse().find((event) => event.step !== 'model_change')?.step || ''
    if (step.includes('review')) return runAction('评审', reviewTask)
    if (step.includes('optimize')) return runAction('优化 Prompt', optimizePromptTask)
    if (step.includes('regenerat')) return onRegenerate()
    return runAction('生成', generateTask)
  }

  async function onSwitchModelAndRetry(modelId: number) {
    const currentTask = options.task.value
    if (!currentTask) return
    acting.value = true
    try {
      const updated = await updateTaskModel(currentTask.id, modelId)
      if (!options.applyTaskUpdate(updated)) return
      retryModelDialogVisible.value = false
      ElMessage.success('模型已切换，正在重新生成')
    } catch (e) {
      if (currentTask.id === options.taskId.value) {
        ElMessage.error(`切换模型失败：${(e as Error).message}`)
      }
      return
    } finally {
      acting.value = false
    }
    if (currentTask.id !== options.taskId.value) return
    await runAction('生成', generateTask)
  }

  function openApplyDialog(rev?: PromptRevision) {
    selectedRevision.value = rev || options.pendingRevisions.value[0] || options.revisions.value[0] || null
    if (!selectedRevision.value) {
      ElMessage.warning('没有可应用的 Prompt 修订')
      return
    }
    applyDialogVisible.value = true
  }

  async function confirmApply(payload: ApplyPromptPayload) {
    const currentTask = options.task.value
    if (!currentTask || !selectedRevision.value) return
    acting.value = true
    try {
      const applied = await applyPrompt(currentTask.id, {
        revision_id: selectedRevision.value.id,
        mode: 'task_temp',
        content: payload.content,
      })
      if (!options.applyTaskUpdate(applied)) return
      ElMessage.success('已仅对本任务应用临时 Prompt')
      applyDialogVisible.value = false
      if (payload.alsoRegenerate) {
        const regenerated = await regenerateTask(currentTask.id)
        if (!options.applyTaskUpdate(regenerated)) return
        ElMessage.success('已触发再生成')
      }
      await options.refreshLight(true)
    } catch (e) {
      if (currentTask.id === options.taskId.value) {
        ElMessage.error(`应用失败：${(e as Error).message}`)
      }
    } finally {
      acting.value = false
    }
  }

  return {
    acting,
    confirmingCheckpoint,
    confirmingTestPoints,
    applyDialogVisible,
    selectedRevision,
    retryModelDialogVisible,
    finalizeConflictDialogVisible,
    finalizeConflicts,
    pendingFinalizePendingCount,
    resetDialogState,
    confirmCheckpoint,
    saveTestPoints,
    confirmTestPointStage,
    runAction,
    onGenerate: () => runAction('生成', generateTask),
    onReview: () => runAction('评审', reviewTask),
    onOptimize: () => runAction('优化 Prompt', optimizePromptTask),
    onStopGeneration,
    onRegenerate,
    onFinalize,
    confirmFinalizeOverwrite,
    onRetryFailed,
    onSwitchModelAndRetry,
    openApplyDialog,
    confirmApply,
  }
}
