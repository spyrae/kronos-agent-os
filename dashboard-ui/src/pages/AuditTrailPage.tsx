import { useEffect, useState } from 'react';
import { api } from '../api/client';
import { EventTypeBadge } from '../components/Charts';

interface AuditEvent {
  id: string; type: string; agent: string;
  description: string; timestamp: string;
  metadata: { duration_ms?: number; cost_usd?: number };
}

const EVENT_TYPES = ['all', 'WRITE', 'SEARCH', 'DECISION', 'ERROR', 'CRASH', 'RECOVERY'] as const;
const TYPE_COLORS: Record<string, string> = {
  DECISION: '#2563eb', WRITE: '#f97316', SEARCH: '#8b5cf6',
  ERROR: '#ef4444', CRASH: '#ef4444', RECOVERY: '#4ade80',
};

function timeAgo(ts: string): string {
  if (!ts) return '';
  const diff = Date.now() - new Date(ts).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins} min ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

export default function AuditTrailPage() {
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [filter, setFilter] = useState('all');
  const [total, setTotal] = useState(0);

  useEffect(() => {
    api<{ events: AuditEvent[]; counts: Record<string, number>; total: number }>(
      `/api/audit-trail/events?type=${filter}&limit=50`
    ).then(r => {
      setEvents(r.events);
      setCounts(r.counts);
      setTotal(r.total);
    }).catch(() => {});
  }, [filter]);

  const card: React.CSSProperties = {
    background: '#111', border: '1px solid #1a1a1a', borderRadius: '12px', padding: '1.25rem',
  };

  return (
    <div>
      <h1 style={{ fontSize: '1.5rem', fontWeight: 600, marginBottom: '1.25rem' }}>Audit Trail</h1>

      {/* Filter pills */}
      <div style={{ display: 'flex', gap: '0.3rem', marginBottom: '1rem', flexWrap: 'wrap' }}>
        {EVENT_TYPES.map(t => (
          <button key={t} onClick={() => setFilter(t)} style={{
            padding: '0.4rem 0.85rem', borderRadius: 20, cursor: 'pointer',
            background: filter === t ? (t === 'all' ? '#f97316' : `${TYPE_COLORS[t] || '#666'}33`) : '#111',
            color: filter === t ? '#fff' : '#777',
            fontSize: '0.78rem', fontWeight: filter === t ? 600 : 400,
            border: `1px solid ${filter === t ? 'transparent' : '#1a1a1a'}`,
          }}>{t === 'all' ? 'All' : t}</button>
        ))}
      </div>

      {/* Summary bar */}
      <div style={{ ...card, marginBottom: '1rem', display: 'flex', gap: '1.5rem', alignItems: 'center', flexWrap: 'wrap' }}>
        {Object.entries(counts).map(([type, count]) => (
          <div key={type} style={{ display: 'flex', alignItems: 'center', gap: '0.4rem' }}>
            <span style={{
              fontSize: '1.1rem', fontWeight: 700,
              color: TYPE_COLORS[type] || '#888',
            }}>{count}</span>
            <span style={{ fontSize: '0.75rem', color: '#888', textTransform: 'uppercase' }}>{type}</span>
          </div>
        ))}
        {Object.keys(counts).length === 0 && (
          <span style={{ color: '#555', fontSize: '0.85rem' }}>No events recorded</span>
        )}
        <span style={{ marginLeft: 'auto', fontSize: '0.72rem', color: '#555' }}>{total} total events</span>
      </div>

      {/* Events timeline */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: '0.4rem' }}>
        {events.map(evt => (
          <div key={evt.id} style={{
            ...card, padding: '0.85rem 1rem',
            borderLeft: `3px solid ${TYPE_COLORS[evt.type] || '#333'}`,
            display: 'flex', alignItems: 'flex-start', gap: '0.75rem',
          }}>
            <div style={{ flexShrink: 0, marginTop: 2 }}>
              <EventTypeBadge type={evt.type} />
            </div>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{
                fontSize: '0.85rem', color: '#e0e0e0', marginBottom: '0.2rem',
                overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
              }}>{evt.description || 'No description'}</div>
              <div style={{ display: 'flex', gap: '0.75rem', fontSize: '0.7rem', color: '#555' }}>
                <span>agent: {evt.agent}</span>
                {evt.metadata.duration_ms && <span>{evt.metadata.duration_ms.toFixed(0)}ms</span>}
                {evt.metadata.cost_usd && <span>${evt.metadata.cost_usd.toFixed(5)}</span>}
              </div>
            </div>
            <div style={{ fontSize: '0.7rem', color: '#444', flexShrink: 0, whiteSpace: 'nowrap' }}>
              {timeAgo(evt.timestamp)}
            </div>
          </div>
        ))}
        {events.length === 0 && (
          <div style={{ ...card, textAlign: 'center', color: '#555', padding: '3rem' }}>
            No {filter === 'all' ? '' : filter.toLowerCase() + ' '}events found
          </div>
        )}
      </div>
    </div>
  );
}
