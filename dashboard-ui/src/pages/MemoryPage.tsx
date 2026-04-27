import { useEffect, useState } from 'react';
import type { CSSProperties } from 'react';
import { api } from '../api/client';
import { SectionHeader, StatusBadge } from '../components/Charts';

interface MemoryRecord {
  id: string;
  type: string;
  source: string;
  memory: string;
  created_at: string;
  updated_at: string;
  user_id: string;
  session_id: string;
  template: string;
  metadata: Record<string, unknown>;
  recall_reason: string;
}

interface MemoryStatus {
  status: string;
  total_memories: number;
  qdrant: string;
  error?: string;
  counts?: Record<string, number>;
}

interface RecordsResponse {
  records: MemoryRecord[];
  total: number;
  filters: { types: string[]; sources: string[]; sessions: string[] };
}

const STORE_COLORS: Record<string, string> = {
  fact: '#4ade80',
  shared_fact: '#06b6d4',
  entity: '#8b5cf6',
  relation: '#f97316',
  session: '#3b82f6',
};

const RESET_SCOPES = ['facts', 'shared', 'knowledge_graph', 'sessions', 'all'];

function formatDate(value: string) {
  if (!value) return '-';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString([], { month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit' });
}

function statusFor(value: string): 'running' | 'stopped' | 'error' | 'warning' {
  if (value === 'ok') return 'running';
  if (value === 'degraded') return 'warning';
  if (value === 'error') return 'error';
  return 'stopped';
}

function SelectFilter({
  value,
  options,
  onChange,
}: {
  value: string;
  options: string[];
  onChange: (value: string) => void;
}) {
  return (
    <select value={value} onChange={e => onChange(e.target.value)} style={{
      background: '#0a0a0a', color: '#ddd', border: '1px solid #222', borderRadius: 6,
      padding: '0.5rem 0.65rem', fontSize: '0.8rem', minWidth: 140,
    }}>
      <option value="all">All</option>
      {options.map(option => <option key={option} value={option}>{option}</option>)}
    </select>
  );
}

export default function MemoryPage() {
  const [status, setStatus] = useState<MemoryStatus | null>(null);
  const [records, setRecords] = useState<MemoryRecord[]>([]);
  const [filters, setFilters] = useState<RecordsResponse['filters']>({ types: [], sources: [], sessions: [] });
  const [selected, setSelected] = useState<MemoryRecord | null>(null);
  const [query, setQuery] = useState('');
  const [typeFilter, setTypeFilter] = useState('all');
  const [sourceFilter, setSourceFilter] = useState('all');
  const [sessionFilter, setSessionFilter] = useState('all');
  const [addText, setAddText] = useState('');
  const [showAdd, setShowAdd] = useState(false);
  const [resetScope, setResetScope] = useState('facts');
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState('');

  const showToast = (msg: string) => { setToast(msg); setTimeout(() => setToast(''), 3000); };

  const fetchData = async () => {
    try {
      const params = new URLSearchParams({
        query,
        type: typeFilter,
        source: sourceFilter,
        session: sessionFilter,
        limit: '300',
      });
      const [statusR, recordsR] = await Promise.all([
        api<MemoryStatus>('/api/memory/status'),
        api<RecordsResponse>(`/api/memory/records?${params.toString()}`),
      ]);
      setStatus(statusR);
      setRecords(recordsR.records);
      setFilters(recordsR.filters);
      if (selected && !recordsR.records.some(item => item.id === selected.id)) {
        setSelected(null);
      }
    } catch {
      showToast('Memory load failed');
    }
  };

  useEffect(() => { fetchData(); }, [query, typeFilter, sourceFilter, sessionFilter]);

  const doAdd = async () => {
    if (!addText.trim()) return;
    setBusy(true);
    try {
      await api('/api/memory/add', { method: 'POST', body: JSON.stringify({ text: addText }) });
      showToast('Memory added');
      setAddText('');
      setShowAdd(false);
      fetchData();
    } catch { showToast('Add failed'); }
    setBusy(false);
  };

  const deleteSelected = async () => {
    if (!selected) return;
    if (!window.confirm(`Delete ${selected.id}?`)) return;
    setBusy(true);
    try {
      await api(`/api/memory/records/${encodeURIComponent(selected.id)}`, { method: 'DELETE' });
      showToast('Memory deleted');
      setSelected(null);
      fetchData();
    } catch { showToast('Delete failed'); }
    setBusy(false);
  };

  const resetMemory = async () => {
    if (!window.confirm(`Reset ${resetScope}?`)) return;
    setBusy(true);
    try {
      await api('/api/memory/reset', { method: 'POST', body: JSON.stringify({ scope: resetScope, confirm: true }) });
      showToast(`${resetScope} reset`);
      setSelected(null);
      fetchData();
    } catch { showToast('Reset failed'); }
    setBusy(false);
  };

  const card: CSSProperties = {
    background: '#111', border: '1px solid #1a1a1a', borderRadius: 8, padding: '1.1rem',
  };
  const input: CSSProperties = {
    padding: '0.5rem 0.75rem', background: '#0a0a0a', border: '1px solid #222',
    borderRadius: 6, color: '#e0e0e0', fontSize: '0.85rem', outline: 'none',
  };

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1.25rem', gap: '1rem' }}>
        <h1 style={{ fontSize: '1.5rem', fontWeight: 600 }}>Memory Inspector</h1>
        {status && <StatusBadge status={statusFor(status.status)} label={status.status} />}
      </div>

      {status && (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(145px, 1fr))', gap: '0.75rem', marginBottom: '1rem' }}>
          {Object.entries(status.counts || {}).map(([key, value]) => (
            <div key={key} style={{ ...card, padding: '0.8rem 0.9rem' }}>
              <div style={{ color: '#666', fontSize: '0.68rem', textTransform: 'uppercase', letterSpacing: '0.04em', marginBottom: '0.3rem' }}>{key.replace(/_/g, ' ')}</div>
              <div style={{ color: '#fff', fontSize: '1.25rem', fontWeight: 750 }}>{value}</div>
            </div>
          ))}
        </div>
      )}

      <div style={{ ...card, marginBottom: '1rem' }}>
        <div style={{ display: 'flex', gap: '0.65rem', flexWrap: 'wrap', alignItems: 'center' }}>
          <input
            style={{ ...input, flex: '1 1 260px' }}
            placeholder="Search memory"
            value={query}
            onChange={e => setQuery(e.target.value)}
          />
          <SelectFilter value={typeFilter} options={filters.types} onChange={setTypeFilter} />
          <SelectFilter value={sourceFilter} options={filters.sources} onChange={setSourceFilter} />
          <SelectFilter value={sessionFilter} options={filters.sessions} onChange={setSessionFilter} />
          <button onClick={() => setShowAdd(!showAdd)} style={{
            padding: '0.5rem 0.85rem', borderRadius: 6, border: '1px solid #2a2a2a',
            background: showAdd ? '#1a1a1a' : '#f97316', color: '#fff', cursor: 'pointer', fontWeight: 650,
          }}>{showAdd ? 'Cancel' : 'New'}</button>
        </div>
        {showAdd && (
          <div style={{ display: 'flex', gap: '0.5rem', marginTop: '0.75rem' }}>
            <input
              style={{ ...input, flex: 1 }}
              placeholder="Add durable fact"
              value={addText}
              onChange={e => setAddText(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter') doAdd(); }}
            />
            <button onClick={doAdd} disabled={busy} style={{
              padding: '0.5rem 1rem', borderRadius: 6, border: 'none',
              background: busy ? '#333' : '#f97316', color: '#fff', cursor: busy ? 'default' : 'pointer', fontWeight: 650,
            }}>Add</button>
          </div>
        )}
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) 390px', gap: '1rem' }}>
        <div style={card}>
          <SectionHeader title={`Records (${records.length})`} />
          <div style={{ maxHeight: 'calc(100vh - 420px)', minHeight: 260, overflow: 'auto', display: 'flex', flexDirection: 'column', gap: '0.4rem' }}>
            {records.length === 0 && (
              <div style={{ padding: '2rem', color: '#555', textAlign: 'center', fontSize: '0.85rem' }}>
                Demo: launch reviewers prefer concise technical answers.
              </div>
            )}
            {records.map(record => (
              <div
                key={record.id}
                onClick={() => setSelected(record)}
                style={{
                  padding: '0.7rem 0.75rem', borderRadius: 8, cursor: 'pointer',
                  background: selected?.id === record.id ? '#151a22' : '#0a0a0a',
                  border: `1px solid ${selected?.id === record.id ? '#3b82f655' : '#151515'}`,
                }}
              >
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginBottom: '0.35rem' }}>
                  <span style={{ width: 7, height: 7, borderRadius: '50%', background: STORE_COLORS[record.type] || '#777', flexShrink: 0 }} />
                  <span style={{ color: '#888', fontSize: '0.68rem', textTransform: 'uppercase', letterSpacing: '0.04em' }}>{record.type}</span>
                  <span style={{ color: '#555', fontSize: '0.68rem' }}>{record.source}</span>
                </div>
                <div style={{ color: '#ddd', fontSize: '0.84rem', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{record.memory}</div>
                <div style={{ marginTop: '0.3rem', color: '#555', fontSize: '0.68rem' }}>{record.session_id || record.user_id || record.template} · {formatDate(record.updated_at)}</div>
              </div>
            ))}
          </div>
        </div>

        <div style={card}>
          <SectionHeader title="Detail" />
          {selected ? (
            <div>
              <div style={{ background: '#0a0a0a', border: '1px solid #1a1a1a', borderRadius: 8, padding: '0.9rem', marginBottom: '0.9rem' }}>
                <div style={{ color: '#e5e5e5', fontSize: '0.9rem', lineHeight: 1.55, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>{selected.memory}</div>
              </div>
              {[
                ['ID', selected.id],
                ['Type', selected.type],
                ['Source', selected.source],
                ['Session', selected.session_id || '-'],
                ['User', selected.user_id || '-'],
                ['Template', selected.template || '-'],
                ['Created', formatDate(selected.created_at)],
                ['Updated', formatDate(selected.updated_at)],
              ].map(([label, value]) => (
                <div key={label} style={{ display: 'grid', gridTemplateColumns: '78px 1fr', gap: '0.6rem', marginBottom: '0.42rem', fontSize: '0.74rem' }}>
                  <span style={{ color: '#555', textTransform: 'uppercase', letterSpacing: '0.04em' }}>{label}</span>
                  <span style={{ color: '#aaa', overflowWrap: 'anywhere' }}>{value}</span>
                </div>
              ))}
              <div style={{ marginTop: '0.85rem' }}>
                <div style={{ color: '#666', fontSize: '0.68rem', textTransform: 'uppercase', letterSpacing: '0.04em', marginBottom: '0.35rem' }}>Recall</div>
                <div style={{ color: '#999', fontSize: '0.78rem', lineHeight: 1.45 }}>{selected.recall_reason || '-'}</div>
              </div>
              <div style={{ marginTop: '0.85rem' }}>
                <div style={{ color: '#666', fontSize: '0.68rem', textTransform: 'uppercase', letterSpacing: '0.04em', marginBottom: '0.35rem' }}>Metadata</div>
                <pre style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word', color: '#888', background: '#050505', border: '1px solid #181818', borderRadius: 6, padding: '0.65rem', fontSize: '0.72rem', maxHeight: 180, overflow: 'auto' }}>{JSON.stringify(selected.metadata, null, 2)}</pre>
              </div>
              <button onClick={deleteSelected} disabled={busy} style={{
                marginTop: '0.85rem', width: '100%', padding: '0.55rem 0.8rem', borderRadius: 6,
                border: '1px solid #7f1d1d', background: '#1a0505', color: '#fca5a5',
                cursor: busy ? 'default' : 'pointer', fontWeight: 650,
              }}>Delete Record</button>
            </div>
          ) : (
            <div style={{ padding: '2rem', textAlign: 'center', color: '#555', fontSize: '0.85rem' }}>Select a record</div>
          )}
        </div>
      </div>

      <div style={{ ...card, marginTop: '1rem', display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '0.75rem', flexWrap: 'wrap' }}>
        <div style={{ display: 'flex', gap: '0.6rem', alignItems: 'center' }}>
          <span style={{ color: '#777', fontSize: '0.8rem' }}>Reset</span>
          <select value={resetScope} onChange={e => setResetScope(e.target.value)} style={{
            background: '#0a0a0a', color: '#ddd', border: '1px solid #222', borderRadius: 6,
            padding: '0.5rem 0.65rem', fontSize: '0.8rem', minWidth: 160,
          }}>
            {RESET_SCOPES.map(scope => <option key={scope} value={scope}>{scope}</option>)}
          </select>
        </div>
        <button onClick={resetMemory} disabled={busy} style={{
          padding: '0.5rem 0.85rem', borderRadius: 6, border: '1px solid #7f1d1d',
          background: '#1a0505', color: '#fca5a5', cursor: busy ? 'default' : 'pointer', fontWeight: 650,
        }}>Reset Scope</button>
      </div>

      {toast && (
        <div style={{
          position: 'fixed', bottom: '1.5rem', right: '1.5rem',
          background: '#1a1a2e', color: '#fff', padding: '0.75rem 1.5rem',
          borderRadius: 8, fontSize: '0.85rem', zIndex: 9999,
          boxShadow: '0 4px 20px rgba(0,0,0,0.4)', border: '1px solid #2563eb33',
        }}>{toast}</div>
      )}
    </div>
  );
}
