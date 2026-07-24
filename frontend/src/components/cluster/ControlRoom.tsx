import type { ClusterStatus, DemoAction, DemoEvent, ReplicaStatus } from '../../types/demo'

interface Props {
  status: ClusterStatus | null
  error: string
  events: DemoEvent[]
  pendingAction: DemoAction | ''
  onAction: (action: DemoAction) => void
}

function Replica({ replica }: { replica: ReplicaStatus }) {
  return (
    <div className={`replica ${replica.healthy ? '' : 'down'}`} data-testid={replica.role}>
      <span className="replica-dot" />
      <div><strong>{replica.id}</strong><small>{replica.role.replace('-', ' ')}</small></div>
      <span className={`mini-status ${replica.ready ? 'ready' : ''}`}>{replica.healthy ? replica.ready ? 'SYNCED' : 'STANDBY' : 'DOWN'}</span>
    </div>
  )
}

export function ControlRoom({ status, error, events, pendingAction, onAction }: Props) {
  const replicas = status ? [status.primary, status.synchronousBackup, ...status.standbys].filter(Boolean) as ReplicaStatus[] : []
  const metric = (value: number | null | undefined, suffix = '') => value === null || value === undefined ? 'Unknown' : `${value}${suffix}`
  return (
    <section className="control-column">
      <section className="panel topology-panel">
        <div className="section-heading"><div><span className="eyebrow">Live topology</span><h2>Replication control room</h2></div><span className={`status ${status?.state.toLowerCase() ?? 'unknown'}`} data-testid="cluster-state">{status?.state ?? 'UNKNOWN'}</span></div>
        {error && <div className="notice error">{error}</div>}
        <div className="topology">
          {replicas.length ? replicas.map(replica => <Replica key={replica.id} replica={replica} />) : <p className="muted">Waiting for authoritative metrics…</p>}
        </div>
        <div className="protection-strip">
          <div><span>Write path</span><strong>{status?.writesAvailable ? 'Available' : 'Unavailable'}</strong></div>
          <div><span>Protection</span><strong data-testid="protection-status">{status?.protected ? 'Synchronous' : 'Degraded'}</strong></div>
          <div><span>Epoch</span><strong data-testid="epoch">{metric(status?.epoch)}</strong></div>
          <div><span>Watch streams</span><strong>{metric(status?.activeWatchStreams)}</strong></div>
        </div>
        <div className="failure-controls">
          <button onClick={() => onAction('fail-backup')} disabled={Boolean(pendingAction) || !status?.synchronousBackup}>Fail backup</button>
          <button onClick={() => onAction('restart-backup')} disabled={Boolean(pendingAction)}>Restart backup</button>
          <button className="danger" onClick={() => onAction('fail-primary')} disabled={Boolean(pendingAction) || !status?.primary}>Fail primary</button>
          <button onClick={() => onAction('restart-primary')} disabled={Boolean(pendingAction)}>Restart primary</button>
          <button onClick={() => onAction('restart-cluster')} disabled={Boolean(pendingAction)}>Restart cluster</button>
        </div>
        {pendingAction && <p className="pending">Running {pendingAction.replaceAll('-', ' ')}…</p>}
      </section>
      <section className="panel metrics-panel">
        <span className="eyebrow">Prometheus summary</span>
        <div className="metric-grid">
          <div><strong>{metric(status?.metrics.failoversCompleted)}</strong><span>Failovers complete</span></div>
          <div><strong>{metric(status?.metrics.reprotectionsCompleted)}</strong><span>Reprotections</span></div>
          <div><strong>{metric(status?.metrics.lastFailoverSeconds, 's')}</strong><span>Last failover</span></div>
          <div><strong>{metric(status?.metrics.mutationSuccesses)}</strong><span>Mutations accepted</span></div>
        </div>
      </section>
      <section className="panel events-panel">
        <div className="section-heading"><div><span className="eyebrow">Recent events</span><h2>System timeline</h2></div><span className="live-label">LIVE</span></div>
        <ol className="events">
          {events.length ? events.slice(0, 12).map(event => (
            <li key={event.id} className={event.severity}><time>{new Date(event.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}</time><span className="event-dot" /><div><strong>{event.title}</strong>{event.detail && <p>{event.detail}</p>}</div></li>
          )) : <li className="muted">Events appear as the demo runs.</li>}
        </ol>
      </section>
    </section>
  )
}
