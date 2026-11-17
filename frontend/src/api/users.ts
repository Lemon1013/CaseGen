import { api } from './client'

export interface UserItem {
  id: number
  username: string
  display_name: string
  role: 'admin' | 'user'
  is_active: boolean
  created_at: string
  updated_at: string
}

export const listUsers = () => api<UserItem[]>('/api/users')

export const createUser = (body: {
  username: string
  display_name?: string
  password: string
  role?: 'admin' | 'user'
}) => api<UserItem>('/api/users', { method: 'POST', body: JSON.stringify(body) })

export const updateUser = (
  id: number,
  body: { display_name?: string; role?: 'admin' | 'user'; is_active?: boolean },
) => api<UserItem>(`/api/users/${id}`, { method: 'PATCH', body: JSON.stringify(body) })

export const resetUserPassword = (id: number, password: string) =>
  api<UserItem>(`/api/users/${id}/reset-password`, {
    method: 'POST',
    body: JSON.stringify({ password }),
  })
