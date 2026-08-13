import { api } from './client'

export interface AuthUser {
  id: number
  username: string
  display_name: string
  role: string
}

export interface BootstrapResponse {
  setup_required: boolean
  user: AuthUser | null
}

export interface AuthSession {
  user: AuthUser
  expires_at: string
}

export function getAuthBootstrap() {
  return api<BootstrapResponse>('/api/auth/bootstrap')
}

export function setupAccount(body: { username: string; display_name?: string; password: string }) {
  return api<AuthSession>('/api/auth/setup', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

export function login(body: { username: string; password: string }) {
  return api<AuthSession>('/api/auth/login', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

export function logout() {
  return api<void>('/api/auth/logout', { method: 'POST' })
}

export function getSession() {
  return api<AuthSession>('/api/auth/session')
}
