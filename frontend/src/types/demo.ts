import type { AuctionResult, PublicAuction } from '../proto/blindsided'

export type ClusterState = 'READY' | 'REPROTECTING' | 'FAILING_OVER' | 'UNAVAILABLE'
export type WatchState = 'connected' | 'reconnecting' | 'disconnected' | 'complete' | 'error'

export interface ReplicaStatus {
  id: string
  role: 'primary' | 'synchronous-backup' | 'standby'
  healthy: boolean
  ready: boolean
  epoch: number | null
}

export interface ClusterMetrics {
  failoversCompleted: number | null
  reprotectionsCompleted: number | null
  lastFailoverSeconds: number | null
  mutationSuccesses: number | null
  mutationFailures: number | null
  serviceReplicas: number | null
}

export interface ClusterStatus {
  state: ClusterState
  epoch: number | null
  primary: ReplicaStatus | null
  synchronousBackup: ReplicaStatus | null
  standbys: ReplicaStatus[]
  protected: boolean
  writesAvailable: boolean
  activeWatchStreams: number | null
  metrics: ClusterMetrics
  observedAt: string
}

export interface DemoEvent {
  id: string
  timestamp: string
  category: 'auction' | 'replication' | 'failover' | 'watch' | 'system'
  title: string
  detail?: string
  severity: 'info' | 'success' | 'warning' | 'critical'
}

export interface AuctionView {
  auction: PublicAuction
  version: number
  ownActiveBid?: number
  result?: AuctionResult
}

export type DemoAction =
  | 'fail-backup'
  | 'restart-backup'
  | 'fail-primary'
  | 'restart-primary'
  | 'restart-cluster'
