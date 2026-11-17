import { computed, reactive } from 'vue'
import type { LocationQuery, Router } from 'vue-router'
import { ApiError } from './api/client'
import { listProjects, type Project } from './api/projects'

const STORAGE_KEY = 'casegen:last-project-id'
const state = reactive({ projects: [] as Project[], currentId: null as number | null, loaded: false })

/** Clear the module-level singleton so a new login cannot inherit the previous user's scope. */
export function resetProjectStoreState() {
  state.projects = []
  state.currentId = null
  state.loaded = false
}

function queryId(query: LocationQuery) {
  const raw = Array.isArray(query.project_id) ? query.project_id[0] : query.project_id
  const value = Number(raw)
  return Number.isInteger(value) && value > 0 ? value : null
}

function querySpaceId(query: LocationQuery) {
  const raw = Array.isArray(query.space_id) ? query.space_id[0] : query.space_id
  const value = Number(raw)
  return Number.isInteger(value) && value > 0 ? value : null
}

function activeSpaceId(project: Project | null, requested: number | null) {
  if (!project) return undefined
  const privateSpace = project.private_spaces.find((item) => item.id === (requested ?? project.default_wiki_space_id) && item.status === 'active' && item.scope === 'project')
  if (privateSpace) return privateSpace.id
  if (requested !== null) {
    const sharedSpace = project.shared_spaces.find((item) => item.id === requested && item.status === 'active' && item.enabled)
    if (sharedSpace) return sharedSpace.id
  }
  return project.private_spaces.find((item) => item.id === project.default_wiki_space_id && item.status === 'active' && item.scope === 'project')?.id
}

async function replaceProjectQuery(router: Router, projectId: number | null, spaceId: number | undefined) {
  const query = router.currentRoute.value.query
  const currentProjectId = Array.isArray(query.project_id) ? query.project_id[0] : query.project_id
  const currentSpaceId = Array.isArray(query.space_id) ? query.space_id[0] : query.space_id
  const nextProjectId = projectId ? String(projectId) : undefined
  const nextSpaceId = spaceId ? String(spaceId) : undefined
  if ((currentProjectId ?? undefined) === nextProjectId && (currentSpaceId ?? undefined) === nextSpaceId) return
  await router.replace({ query: { ...query, project_id: nextProjectId, space_id: nextSpaceId } })
}

/**
 * Load projects, recovering once from a stale/hidden cached project id.
 * client.ts injects localStorage's project_id into every /api request, so a
 * hidden or revoked id can make even the list endpoint fail.  Dropping the
 * cached id and retrying lets the user switch to a visible project.
 */
async function loadProjectsWithRecovery(): Promise<Project[]> {
  try {
    return await listProjects()
  } catch (error) {
    const status = error instanceof ApiError ? error.status : 0
    if (status !== 404 && status !== 401) throw error
    if (!localStorage.getItem(STORAGE_KEY)) throw error
    localStorage.removeItem(STORAGE_KEY)
    return listProjects()
  }
}

export function useProjectStore() {
  const current = computed(() => state.projects.find((item) => item.id === state.currentId) || null)
  // A stale project id can survive in a bookmarked URL while its wiki space
  // still points at the canonical project after a duplicate merge.
  function projectForSpace(spaceId: number | null) {
    if (spaceId === null) return null
    return state.projects.find((project) =>
      project.status === 'active' &&
      project.private_spaces.some((space) => space.id === spaceId && space.status === 'active' && space.scope === 'project'),
    ) ?? null
  }
  async function load(query: LocationQuery) {
    state.projects = await loadProjectsWithRecovery()
    const requested = queryId(query) || Number(localStorage.getItem(STORAGE_KEY))
    const project =
      state.projects.find((item) => item.id === requested && item.status === 'active') ||
      projectForSpace(querySpaceId(query)) ||
      state.projects.find((item) => item.status === 'active') ||
      null
    state.currentId = project?.id || null
    if (project) localStorage.setItem(STORAGE_KEY, String(project.id))
    state.loaded = true
  }
  async function select(id: number, router: Router, requestedSpace: number | null = null) {
    const project = state.projects.find((item) => item.id === id && item.status === 'active')
    if (!project) return
    localStorage.setItem(STORAGE_KEY, String(id))
    await replaceProjectQuery(router, id, activeSpaceId(project, requestedSpace))
    state.currentId = id
  }
  async function refresh(router?: Router) {
    state.projects = await loadProjectsWithRecovery()
    const requestedSpace = router ? querySpaceId(router.currentRoute.value.query) : null
    const requestedProject = router ? queryId(router.currentRoute.value.query) : null
    const requestedProjectRow = requestedProject === null ? null : state.projects.find((item) => item.id === requestedProject && item.status === 'active') ?? null
    const selected =
      requestedProjectRow ||
      projectForSpace(requestedSpace) ||
      state.projects.find((item) => item.id === state.currentId && item.status === 'active') ||
      state.projects.find((item) => item.status === 'active') ||
      null
    state.currentId = selected?.id || null
    if (selected) localStorage.setItem(STORAGE_KEY, String(selected.id))
    else localStorage.removeItem(STORAGE_KEY)
    state.loaded = true
    if (router) {
      await replaceProjectQuery(router, selected?.id ?? null, activeSpaceId(selected, requestedSpace))
    }
  }
  // Rewrite missing or stale project ids after the project list is reloaded.
  async function ensureRouteProject(router: Router) {
    if (!state.loaded) return
    const requested = queryId(router.currentRoute.value.query)
    const valid = requested !== null && state.projects.some((item) => item.id === requested && item.status === 'active')
    if (valid) {
      if (requested !== state.currentId) {
        state.currentId = requested
        localStorage.setItem(STORAGE_KEY, String(requested))
      }
      return
    }
    const requestedSpace = querySpaceId(router.currentRoute.value.query)
    const bySpace = projectForSpace(requestedSpace)
    if (bySpace) {
      await select(bySpace.id, router, requestedSpace)
      return
    }
    if (state.currentId) await select(state.currentId, router)
  }
  return { state, current, load, reload: refresh, refresh, select, ensureRouteProject }
}
