import type { LocationQuery, Router } from 'vue-router'
import type { WikiSpace } from '../api/wikiSpaces'

export const WIKI_SPACE_STORAGE_KEY = 'casegen:last-wiki-space-id'

export function spaceIdFromQuery(query: LocationQuery): number | null {
  const raw = Array.isArray(query.space_id) ? query.space_id[0] : query.space_id
  const id = Number(raw)
  return Number.isInteger(id) && id > 0 ? id : null
}

export function rememberedSpaceId(): number | null {
  const id = Number(localStorage.getItem(WIKI_SPACE_STORAGE_KEY))
  return Number.isInteger(id) && id > 0 ? id : null
}

export function chooseSpace(spaces: WikiSpace[], requested: number | null): WikiSpace | null {
  const active = spaces.filter((space) => space.status === 'active')
  return (
    active.find((space) => space.id === requested) ||
    active.find((space) => space.id === rememberedSpaceId()) ||
    active[0] ||
    spaces.find((space) => space.id === requested) ||
    null
  )
}

export async function rememberAndRoute(router: Router, id: number, path: string) {
  localStorage.setItem(WIKI_SPACE_STORAGE_KEY, String(id))
  await router.replace({ path, query: { space_id: String(id) } })
}
