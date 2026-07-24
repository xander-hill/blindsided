import { useEffect, useState } from 'react'
import { getClusterStatus } from '../services/demoControlClient'
import type { ClusterStatus } from '../types/demo'

export function useClusterStatus() {
  const [status, setStatus] = useState<ClusterStatus | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let active = true
    const refresh = async () => {
      try {
        const next = await getClusterStatus()
        if (active) { setStatus(next); setError('') }
      } catch (cause) {
        if (active) setError(cause instanceof Error ? cause.message : 'Control service unavailable')
      }
    }
    void refresh()
    const timer = window.setInterval(refresh, 2000)
    return () => { active = false; window.clearInterval(timer) }
  }, [])
  return { status, error }
}
