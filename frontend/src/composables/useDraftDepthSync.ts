import { nextTick, onMounted, onUnmounted, ref, watch, type WatchSource } from 'vue'

/**
 * 同步左列草稿滚动区域与右列辅助栏底部的视觉高度。
 */
export function useDraftDepthSync(sources: WatchSource[]) {
  const draftScrollStyle = ref<Record<string, string>>({})
  let rightColumnObserver: ResizeObserver | null = null
  let syncRafId: number | null = null
  let isMounted = false

  function syncDraftDepth() {
    if (typeof window === 'undefined') return
    if (window.innerWidth < 992) {
      draftScrollStyle.value = { maxHeight: '540px' }
      return
    }
    const leftCol = document.querySelector('.left-main-col') as HTMLElement | null
    const rightCol = document.querySelector('.right-main-col') as HTMLElement | null
    const draftCard = document.querySelector('.draft-container-card') as HTMLElement | null
    const rightBlocks = rightCol?.querySelectorAll('.block')
    const lastRightCard = rightBlocks?.length
      ? (rightBlocks[rightBlocks.length - 1] as HTMLElement)
      : null
    const draftScrollCandidates = draftCard
      ? Array.from(draftCard.querySelectorAll<HTMLElement>('.draft-scroll'))
      : []
    const draftScroll = draftScrollCandidates.find(
      (el) => el.offsetParent !== null || el.getBoundingClientRect().height > 0,
    ) ?? null

    const isSideBySide = Boolean(
      leftCol
      && rightCol
      && rightCol.getBoundingClientRect().top < (draftCard?.getBoundingClientRect().bottom ?? 0),
    )
    if (!isSideBySide) {
      draftScrollStyle.value = { maxHeight: '540px' }
      return
    }

    if (draftCard && (lastRightCard || rightCol)) {
      const targetBottom = lastRightCard
        ? lastRightCard.getBoundingClientRect().bottom
        : (rightCol ? rightCol.getBoundingClientRect().bottom : 0)
      const cardBottomOffset = 21
      const scrollTop = draftScroll
        ? draftScroll.getBoundingClientRect().top
        : (draftCard.getBoundingClientRect().top + 156)
      const availableH = Math.round(targetBottom - scrollTop - cardBottomOffset)

      if (availableH >= 380) {
        draftScrollStyle.value = { maxHeight: `${availableH}px`, height: `${availableH}px` }
        return
      }
    }
    draftScrollStyle.value = { maxHeight: '720px' }
  }

  function scheduleSyncDraftDepth() {
    if (typeof window === 'undefined' || !isMounted) return
    if (syncRafId !== null) cancelAnimationFrame(syncRafId)
    syncRafId = requestAnimationFrame(() => {
      syncRafId = null
      if (!isMounted) return
      syncDraftDepth()
    })
  }

  watch(sources, () => {
    nextTick(scheduleSyncDraftDepth)
  })

  onMounted(() => {
    isMounted = true
    window.addEventListener('resize', scheduleSyncDraftDepth)
    const rightCol = document.querySelector('.right-main-col')
    if (rightCol && typeof ResizeObserver !== 'undefined') {
      rightColumnObserver = new ResizeObserver(() => scheduleSyncDraftDepth())
      rightColumnObserver.observe(rightCol)
    }
  })

  onUnmounted(() => {
    isMounted = false
    if (syncRafId !== null) {
      cancelAnimationFrame(syncRafId)
      syncRafId = null
    }
    rightColumnObserver?.disconnect()
    rightColumnObserver = null
    window.removeEventListener('resize', scheduleSyncDraftDepth)
  })

  return { draftScrollStyle, scheduleSyncDraftDepth }
}
