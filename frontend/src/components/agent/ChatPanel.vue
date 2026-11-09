<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { CircleClose, Connection } from '@element-plus/icons-vue'
import { useAgentChatStore } from '../../stores/agentChatStore'
import { hasAgentCaseJson } from './parseAgentCases'
import ChatMessage from './ChatMessage.vue'
import ChatComposer from './ChatComposer.vue'
import type { AgentSaveAsDraftResult } from '../../api/agent'

/**
 * 中列：消息流 + 状态条 + 输入区。
 *
 * 状态全部落在 agentChatStore（spec §3.6-5）：MainLayout.vue:279 的 router-view 用 key 绑定
 * projects.state.currentId，切项目会销毁整个视图；把流式 buffer 放组件里就会被打断。
 * 视图卸载只调 releaseView()：有进行中的轮次就继续 2s 轮询兜底，不 teardown SSE。
 */

const emit = defineEmits<{
  draftSaved: [result: AgentSaveAsDraftResult]
}>()

const store = useAgentChatStore()

const scrollHost = ref<HTMLElement | null>(null)
const stickToBottom = ref(true)

const messages = computed(() => store.displayMessages)
const conversation = computed(() => store.currentConversation)
const progress = computed(() => store.stageProgress)
const busy = computed(() => store.isBusy)
const closed = computed(() => conversation.value?.status === 'closed')

const emptyHint = computed(() => {
  if (store.detailLoading) return '正在载入会话历史…'
  if (messages.value.length) return ''
  return '这条会话还没有消息，直接输入需求开始第一轮对话。'
})

/** 只有「最后一条 assistant 且含用例 JSON」才允许转存草稿（spec §3.5 取最近一条 assistant）。 */
const canSaveDraft = computed(() => {
  if (!conversation.value || closed.value) return false
  if (busy.value) return false
  const list = store.persistedMessages
  for (let index = list.length - 1; index >= 0; index -= 1) {
    if (list[index].role === 'assistant') return hasAgentCaseJson(list[index].content)
  }
  return false
})

function onScroll() {
  const host = scrollHost.value
  if (!host) return
  const distance = host.scrollHeight - host.scrollTop - host.clientHeight
  stickToBottom.value = distance < 80
}

function scrollToBottom(force = false) {
  if (!force && !stickToBottom.value) return
  void nextTick(() => {
    const host = scrollHost.value
    if (!host) return
    host.scrollTop = host.scrollHeight
  })
}

watch(
  () => messages.value.length,
  () => scrollToBottom(true),
)

// 流式期间每帧追加 delta 后跟到底部（rAF 合帧已在 store/composable 内做过）。
watch(
  [() => store.streamingText, () => store.streamingThought],
  () => scrollToBottom(),
)

watch(
  () => store.currentId,
  () => {
    stickToBottom.value = true
    scrollToBottom(true)
  },
)

onMounted(() => scrollToBottom(true))

async function onSend(content: string) {
  try {
    await store.sendMessage(content)
    scrollToBottom(true)
  } catch {
    // store 已写 streamError 并回滚乐观消息，这里只做滚动复位。
    scrollToBottom(true)
  }
}

async function onCancel() {
  try {
    await store.cancelTurn()
  } catch {
    // store 内已记录错误
  }
}

async function onSaveDraft() {
  try {
    const result = await store.saveAsDraft()
    if (result) emit('draftSaved', result)
  } catch {
    // store 内已记录 savingDraftError
  }
}

const composerPlaceholder = computed(() => {
  if (closed.value) return '该会话已关闭，新建会话后可继续对话'
  if (busy.value) return 'Agent 正在生成回复，可等待或中止本轮'
  return undefined
})

const composerDisabled = computed(() =>
  Boolean(closed.value || !conversation.value || !store.agentAvailable),
)
</script>

<template>
  <section class="chat-panel">
    <div v-if="busy || store.streamError" class="panel-status" :class="{ 'is-active': busy }">
      <div class="status-left">
        <el-icon class="status-icon"><Connection /></el-icon>
        <span class="status-text">{{ store.stageMessage || '空闲' }}</span>
      </div>
      <div class="status-right">
        <el-tag v-if="store.streamConnected" size="small" type="success" effect="plain">实时流已连接</el-tag>
        <el-tag v-else-if="store.streamConnecting" size="small" type="warning" effect="plain">连接中…</el-tag>
        <el-tag v-else-if="busy" size="small" type="info" effect="plain">轮询兜底</el-tag>
        <el-tag v-else size="small" type="info" effect="plain">未连接</el-tag>
        <el-tag v-if="closed" size="small" type="danger" effect="plain">会话已关闭</el-tag>
      </div>
    </div>

    <el-progress
      v-if="busy && progress.active"
      class="panel-progress"
      :percentage="progress.determinate ? progress.percent : 0"
      :indeterminate="!progress.determinate"
      :duration="2"
      :stroke-width="4"
      :show-text="progress.determinate"
      :format="() => (progress.determinate ? `${progress.current}/${progress.total}` : '')"
    />

    <el-alert
      v-if="store.streamError"
      class="panel-alert"
      type="error"
      :closable="false"
      show-icon
      :title="store.streamError"
    />
    <el-alert
      v-if="store.savingDraftError"
      class="panel-alert"
      type="warning"
      :closable="false"
      show-icon
      :title="store.savingDraftError"
    />

    <div ref="scrollHost" class="panel-scroll" @scroll="onScroll">
      <div class="msg-column">
        <p v-if="emptyHint" class="panel-empty">{{ emptyHint }}</p>
        <ChatMessage
          v-for="message in messages"
          :key="message.id"
          :role="message.role"
          :content="message.content"
          :thought="message.thought"
          :created-at="message.created_at"
          :streaming="Boolean(message.streaming)"
          :pending="Boolean(message.pending)"
          :can-save-draft="canSaveDraft && !message.streaming && message.role === 'assistant'"
          :saving-draft="store.savingDraft"
          @save-draft="onSaveDraft"
        />
      </div>
    </div>

    <div class="panel-composer">
      <ChatComposer
        :disabled="composerDisabled"
        :sending="store.sending"
        :busy="busy"
        :placeholder="composerPlaceholder"
        :project-id="store.projectId"
        @send="onSend"
      >
        <template #extra-actions>
          <el-button
            v-if="busy"
            type="danger"
            plain
            :loading="store.cancelling"
            @click="onCancel"
          >
            <el-icon><CircleClose /></el-icon>
            <span>中止本轮</span>
          </el-button>
          <el-button
            v-if="canSaveDraft"
            type="primary"
            plain
            :loading="store.savingDraft"
            @click="onSaveDraft"
          >
            转存为用例草稿
          </el-button>
        </template>
      </ChatComposer>
    </div>
  </section>
</template>

<style scoped>
.chat-panel {
  display: flex;
  flex-direction: column;
  gap: 10px;
  flex: 1 1 auto;
  min-height: 0;
}

.panel-status {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 8px 12px;
  border: 1px solid var(--cg-border, #e4e4e7);
  border-radius: var(--cg-radius-sm, 6px);
  background: var(--cg-surface-muted, #f4f4f5);
}

.panel-status.is-active {
  background: var(--cg-accent-brand-soft, #ecfdf5);
  border-color: var(--cg-focus-ring, rgba(16, 185, 129, 0.2));
}

.status-left {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.status-icon {
  color: var(--cg-text-muted, #71717a);
  flex-shrink: 0;
}

.status-text {
  font-size: 12.5px;
  color: var(--cg-text-secondary, #52525b);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.status-right {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
}

.panel-progress {
  padding: 0 2px;
}

.panel-alert {
  padding: 6px 10px;
}

.panel-scroll {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  padding: 8px 12px;
}

.msg-column {
  width: 100%;
  max-width: 900px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.panel-empty {
  margin: 24px auto;
  font-size: 13px;
  color: var(--cg-text-muted, #71717a);
}

.panel-composer {
  flex: 0 0 auto;
  border-top: 1px solid var(--cg-border, #e4e4e7);
  padding-top: 10px;
}
</style>
