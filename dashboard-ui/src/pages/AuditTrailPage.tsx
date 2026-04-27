import { useEffect, useState } from 'react';
import type { CSSProperties } from 'react';
import { api } from '../api/client';
import { EventTypeBadge, StatusBadge } from '../components/Charts';

interface AuditEvent {
  id: string; type: string; agent: string;
  description: string; timestamp: string;
  metadata: { duration_ms?: number; cost_usd?: number };
}

interface ToolCallEvent {
  id: string;
  timestamp: string;
  event: string;
  status: string;
  tool: string;
  capability: string;
  approval_status: string;
  agent: string;
  session_id: string;
  thread_id: string;
  source_kind: string;
  call_id: string;
  turn?: number;
  args_summary: string;
  result_summary: string;
  error: boolean;
  duration_ms?: number;
  cost_usd?: number;
  input_tokens?: number;
  output_tokens?: number;
}

interface ToolCallResponse {
  events: ToolCallEvent[];
  counts: {
    by_status: Record<string, number>;
    by_capability: Record<string, number>;
    by_tool: Record<string, number>;
  };
  filters: {
    sessions: string[];
    tools: string[];
    capabilities: string[];
    statuses: string[];
  };
  total: number;
}

const EVENT_TYPES = ['all', 'WRITE', 'SEARCH', 'DECISION', 'ERROR', 'CRASH', 'RECOVERY'] as const;
const TYPE_COLORS: Record<string, string> = {
  DECISION: '#2563eb', WRITE: '#f97316', SEARCH: '#8b5cf6',
  ERROR: '#ef4444', CRASH: '#ef4444', RECOVERY: '#4ade80',
  TOOL_CALL: '#06b6d4', TOOL_RESULT: '#22c55e', APPROVAL: '#f59e0b',
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

function statusBadge(status: string): 'running' | 'stopped' | 'error' | 'warning' {
  if (status === 'ok') return 'running';
  if (status === 'called') return 'warning';
  if (status === 'error' || status === 'blocked') return 'error';
  return 'stopped';
}

function SelectFilter({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: string[];
  onChange: (value: string) => void;
}) {
  return (
    <label style={{ display: 'flex', flexDirection: 'column', gap: '0.25rem', minWidth: 140 }}>
      <span style={{ color: '#666', fontSize: '0.68rem', textTransform: 'uppercase', letterSpacing: '0.04em' }}>{label}</span>
      <select value={value} onChange={e => onChange(e.target.value)} style={{
        background: '#0a0a0a', color: '#ddd', border: '1px solid #222', borderRadius: 6,
        padding: '0.45rem 0.55rem', fontSize: '0.78rem',
      }}>
        <option value="all">All</option>
        {options.map(option => <option key={option} value={option}>{option}</option>)}
      </select>
    </label>
  );
}

export default function AuditTrailPage() {
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [filter, setFilter] = useState('all');
  const [total, setTotal] = useState(0);
  const [toolCalls, setToolCalls] = useState<ToolCallEvent[]>([]);
  const [toolResponse, setToolResponse] = useState<ToolCallResponse | null>(null);
  const [toolFilter, setToolFilter] = useState('all');
  const [sessionFilter, setSessionFilter] = useState('all');
  const [statusFilter, setStatusFilter] = useState('all');
  const [capabilityFilter, setCapabilityFilter] = useState('all');
  const [selectedToolEvent, setSelectedToolEvent] = useState<ToolCallEvent | null>(null);

  useEffect(() => {
    api<{ events: AuditEvent[]; counts: Record<string, number>; total: number }>(
      `/api/audit-trail/events?type=${filter}&limit=50`
    ).then(r => {
      setEvents(r.events);
      setCounts(r.counts);
      setTotal(r.total);
    }).catch(() => {});
  }, [filter]);

  useEffect(() => {
    const params = new URLSearchParams({
      session: sessionFilter,
      tool: toolFilter,
      status: statusFilter,
      capability: capabilityFilter,
      limit: '100',
    });
    api<ToolCallResponse>(`/api/audit-trail/tool-calls?${params.toString()}`)
      .then(r => {
        setToolCalls(r.events);
        setToolResponse(r);
        if (selectedToolEvent && !r.events.some(event => event.id === selectedToolEvent.id)) {
          setSelectedToolEvent(null);
        }
      })
      .catch(() => {});
  }, [sessionFilter, toolFilter, statusFilter, capabilityFilter, selectedToolEvent]);

  const card: CSSProperties = {
    background: '#111', border: '1px solid #1a1a1a', borderRadius: 8, padding: '1.25rem',
  };

  return (
    <div>
      <h1 style={{ fontSize: '1.5rem', fontWeight: 600, marginBottom: '1.25rem' }}>Audit Trail</h1>

      <div style={{ ...card, marginBottom: '1rem' }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '1rem', marginBottom: '1rem' }}>
          <h2 style={{ fontSize: '0.9rem', fontWeight: 650, color: '#fff', textTransform: 'uppercase', letterSpacing: '0.04em' }}>Tool Calls</h2>
          <span style={{ color: '#777', fontSize: '0.75rem' }}>{toolResponse?.total || 0} events</span>
        </div>

        <div style={{ display: 'flex', gap: '0.75rem', flexWrap: 'wrap', marginBottom: '1rem' }}>
          <SelectFilter label="Session" value={sessionFilter} options={toolResponse?.filters.sessions || []} onChange={setSessionFilter} />
          <SelectFilter label="Tool" value={toolFilter} options={toolResponse?.filters.tools || []} onChange={setToolFilter} />
          <SelectFilter label="Status" value={statusFilter} options={toolResponse?.filters.statuses || []} onChange={setStatusFilter} />
          <SelectFilter label="Capability" value={capabilityFilter} options={toolResponse?.filters.capabilities || []} onChange={setCapabilityFilter} />
        </div>

        <div style={{ display: 'flex', gap: '1rem', flexWrap: 'wrap', marginBottom: '1rem' }}>
          {Object.entries(toolResponse?.counts.by_status || {}).map(([key, value]) => (
            <div key={key} style={{ display: 'flex', alignItems: 'center', gap: '0.4rem' }}>
              <StatusBadge status={statusBadge(key)} label={key} />
              <span style={{ color: '#aaa', fontSize: '0.78rem' }}>{value}</span>
            </div>
          ))}
        </div>

        <div style={{ display: 'grid', gridTemplateColumns: selectedToolEvent ? 'minmax(0, 1fr) 360px' : '1fr', gap: '1rem' }}>
          <div style={{ overflowX: 'auto' }}>
            <table style={{ width: '100%', minWidth: 820, borderCollapse: 'collapse', fontSize: '0.78rem' }}>
              <thead>
                <tr style={{ borderBottom: '1px solid #222' }}>
                  {['Time', 'Event', 'Status', 'Tool', 'Capability', 'Session', 'Latency'].map(header => (
                    <th key={header} style={{ padding: '0.55rem 0.6rem', textAlign: 'left', color: '#555', fontSize: '0.68rem', fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.04em' }}>{header}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {toolCalls.map(event => (
                  <tr
                    key={event.id}
                    onClick={() => setSelectedToolEvent(event)}
                    style={{
                      borderBottom: '1px solid #171717',
                      cursor: 'pointer',
                      background: selectedToolEvent?.id === event.id ? '#17120c' : 'transparent',
                    }}
                  >
                    <td style={{ padding: '0.6rem', color: '#777', whiteSpace: 'nowrap' }}>{timeAgo(event.timestamp)}</td>
                    <td style={{ padding: '0.6rem' }}><EventTypeBadge type={event.event.toUpperCase()} /></td>
                    <td style={{ padding: '0.6rem' }}><StatusBadge status={statusBadge(event.status)} label={event.status} /></td>
                    <td style={{ padding: '0.6rem', color: '#e0e0e0', fontWeight: 600, maxWidth: 220, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{event.tool}</td>
                    <td style={{ padding: '0.6rem', color: '#888' }}>{event.capability}</td>
                    <td style={{ padding: '0.6rem', color: '#888', maxWidth: 130, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{event.session_id || '-'}</td>
                    <td style={{ padding: '0.6rem', color: '#888' }}>{event.duration_ms ? `${event.duration_ms}ms` : '-'}</td>
                  </tr>
                ))}
                {toolCalls.length === 0 && (
                  <tr><td colSpan={7} style={{ padding: '2rem', textAlign: 'center', color: '#555' }}>No tool events found</td></tr>
                )}
              </tbody>
            </table>
          </div>

          {selectedToolEvent && (
            <div style={{ background: '#0a0a0a', border: '1px solid #1f1f1f', borderRadius: 8, padding: '0.9rem', minWidth: 0 }}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '0.75rem', marginBottom: '0.75rem' }}>
                <span style={{ color: '#fff', fontWeight: 650, fontSize: '0.86rem', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{selectedToolEvent.tool}</span>
                <StatusBadge status={statusBadge(selectedToolEvent.status)} label={selectedToolEvent.status} />
              </div>
              {[
                ['Capability', selectedToolEvent.capability],
                ['Approval', selectedToolEvent.approval_status || '-'],
                ['Session', selectedToolEvent.session_id || '-'],
                ['Thread', selectedToolEvent.thread_id || '-'],
                ['Call ID', selectedToolEvent.call_id || '-'],
                ['Agent', selectedToolEvent.agent || '-'],
              ].map(([label, value]) => (
                <div key={label} style={{ display: 'grid', gridTemplateColumns: '86px 1fr', gap: '0.6rem', marginBottom: '0.4rem', fontSize: '0.73rem' }}>
                  <span style={{ color: '#555', textTransform: 'uppercase', letterSpacing: '0.04em' }}>{label}</span>
                  <span style={{ color: '#bbb', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{value}</span>
                </div>
              ))}
              <div style={{ marginTop: '0.9rem' }}>
                <div style={{ color: '#666', fontSize: '0.68rem', textTransform: 'uppercase', letterSpacing: '0.04em', marginBottom: '0.35rem' }}>Args</div>
                <pre style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word', color: '#aaa', background: '#050505', border: '1px solid #181818', borderRadius: 6, padding: '0.65rem', fontSize: '0.72rem', maxHeight: 140, overflow: 'auto' }}>{selectedToolEvent.args_summary || '{}'}</pre>
              </div>
              <div style={{ marginTop: '0.75rem' }}>
                <div style={{ color: '#666', fontSize: '0.68rem', textTransform: 'uppercase', letterSpacing: '0.04em', marginBottom: '0.35rem' }}>Result</div>
                <pre style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word', color: '#aaa', background: '#050505', border: '1px solid #181818', borderRadius: 6, padding: '0.65rem', fontSize: '0.72rem', maxHeight: 180, overflow: 'auto' }}>{selectedToolEvent.result_summary || '-'}</pre>
              </div>
            </div>
          )}
        </div>
      </div>

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
