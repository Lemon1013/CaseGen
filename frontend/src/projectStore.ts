import { computed, reactive } from 'vue'
import type { LocationQuery, Router } from 'vue-router'
import { listProjects, type Project } from './api/projects'

const STORAGE_KEY = 'casegen:last-project-id'
const state = reactive({ projects: [] as Project[], currentId: null as number | null, loaded: false })

function queryId(query: LocationQuery) {
  const raw = Array.isArray(query.project_id) ? query.project_id[0] : query.project_id
  const value = Number(raw)
  return Number.isInteger(value) && value > 0 ? value : null
}

export function useProjectStore() {
  const current = computed(() => state.projects.find((item) => item.id === state.currentId) || null)
  async function load(query: LocationQuery) {
    state.projects = await listProjects()
    const requested = queryId(query) || Number(localStorage.getItem(STORAGE_KEY))
    const project = state.projects.find((item) => item.id === requested && item.status === 'active') || state.projects.find((item) => item.status === 'active') || null
    state.currentId = project?.id || null
    if (project) localStorage.setItem(STORAGE_KEY, String(project.id))
    state.loaded = true
  }
  async function select(id: number, router: Router) {
    const project = state.projects.find((item) => item.id === id)
    if (!project) return
    localStorage.setItem(STORAGE_KEY, String(id))
    await router.replace({ query: { ...router.currentRoute.value.query, project_id: String(id), space_id: project.default_wiki_space_id ? String(project.default_wiki_space_id) : undefined } })
    state.currentId = id
  }
  async function refresh(router?: Router) {
    state.projects = await listProjects()
    const selected = state.projects.find((item) => item.id === state.currentId && item.status === 'active') || state.projects.find((item) => item.status === 'active') || null
    state.currentId = selected?.id || null
    if (selected) localStorage.setItem(STORAGE_KEY, String(selected.id))
    else localStorage.removeItem(STORAGE_KEY)
    state.loaded = true
    if (router) {
      await router.replace({ query: { ...router.currentRoute.value.query, project_id: selected ? String(selected.id) : undefined, space_id: selected?.default_wiki_space_id ? String(selected.default_wiki_space_id) : undefined } })
    }
  }
  return { state, current, load, reload: refresh, refresh, select }
}
