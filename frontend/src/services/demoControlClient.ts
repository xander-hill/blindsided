import { config } from '../app/config'
import type { ClusterStatus, DemoAction, DemoEvent } from '../types/demo'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${config.demoControlUrl}${path}`, {
    cache: 'no-store',
    ...init,
  })
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(payload.error || `Demo control returned ${response.status}`)
  return payload as T
}

export const getClusterStatus = () => request<ClusterStatus>('/demo/status')
export const getSystemEvents = () => request<{ events: DemoEvent[] }>('/demo/events')
export const runDemoAction = (action: DemoAction) =>
  request<{ ok: boolean; message: string }>(`/demo/actions/${action}`, { method: 'POST' })
