import { api } from './client'

export interface AuthUser {
  id: number
  username: string
  display_name: string
  role: 'admin' | 'user'
}

export interface BootstrapResponse {
  setup_required: boolean
  user: AuthUser | null
  registration_enabled: boolean
}

export interface AuthSession {
  user: AuthUser
  expires_at: string
}

export interface RegisterPayload {
  username: string
  display_name: string
  password: string
}

export interface ChangePasswordPayload {
  current_password: string
  new_password: string
}

export interface RegistrationSetting {
  enabled: boolean
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

export function register(body: RegisterPayload) {
  return api<AuthSession>('/api/auth/register', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

export function changePassword(body: ChangePasswordPayload) {
  return api<{ detail: string }>('/api/auth/change-password', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

export function getRegistrationSetting() {
  return api<RegistrationSetting>('/api/auth/registration-setting')
}

export function updateRegistrationSetting(enabled: boolean) {
  return api<RegistrationSetting>('/api/auth/registration-setting', {
    method: 'PUT',
    body: JSON.stringify({ enabled }),
  })
}

export function logout() {
  return api<void>('/api/auth/logout', { method: 'POST' })
}

export function getSession() {
  return api<AuthSession>('/api/auth/session')
}
