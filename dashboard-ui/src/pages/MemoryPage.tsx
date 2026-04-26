import { useEffect, useState } from 'react';
import { api } from '../api/client';
import { SectionHeader } from '../components/Charts';

interface MemoryItem { id: string; memory: string; created_at: string; updated_at: string }
interface MemoryStatus { status: string; total_memories: number; qdrant: string; error?: string }

export default function MemoryPage() {
  const [status, setStatus] = useState<MemoryStatus | null>(null);
  const [memories, setMemories] = useState<MemoryItem[]>([]);
  const [selected, setSelected] = useState<MemoryItem | null>(null);
  const [searchQuery, setSearchQuery] = useState('');
  const [searchResults, setSearchResults] = useState<string[]>([]);
  const [searching, setSearching] = useState(false);
  const [addText, setAddText] = useState('');
  const [adding, setAdding] = useState(false);
  const [showAdd, setShowAdd] = useState(false);
  const [toast, setToast] = useState('');

  const showToast = (msg: string) => { setToast(msg); setTimeout(() => setToast(''), 3000); };

  const fetchData = () => {
    api<MemoryStatus>('/api/memory/status').then(setStatus).catch(() => {});
    api<{ memories: MemoryItem[]; total: number }>('/api/memory/all').then(r => setMemories(r.memories)).catch(() => {});
  };

  useEffect(() => { fetchData(); }, []);

  const doSearch = async () => {
    if (!searchQuery.trim()) { setSearchResults([]); return; }
    setSearching(true);
    try {
      const r = await api<{ results: string[] }>('/api/memory/search', {
        method: 'POST', body: JSON.stringify({ query: searchQuery, limit: 10 }),
      });
      setSearchResults(r.results);
    } catch { showToast('Search failed'); }
    setSearching(false);
  };

  const doAdd = async () => {
    if (!addText.trim()) return;
    setAdding(true);
    try {
      await api('/api/memory/add', { method: 'POST', body: JSON.stringify({ text: addText }) });
      showToast('Memory added');
      setAddText('');
      setShowAdd(false);
      fetchData();
    } catch { showToast('Add failed'); }
    setAdding(false);
  };

  const card: React.CSSProperties = {
    background: '#111', border: '1px solid #1a1a1a', borderRadius: '12px', padding: '1.25rem',
  };
  const input: React.CSSProperties = {
    padding: '0.5rem 0.75rem', background: '#0a0a0a', border: '1px solid #222',
    borderRadius: '6px', color: '#e0e0e0', fontSize: '0.85rem', width: '100%', outline: 'none',
  };

  const displayList = searchResults.length > 0
    ? searchResults.map((r, i) => ({ id: `search-${i}`, memory: r, created_at: '', updated_at: '' }))
    : memories;

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1.25rem' }}>
        <h1 style={{ fontSize: '1.5rem', fontWeight: 600 }}>Memory Explorer</h1>
        <button onClick={() => setShowAdd(!showAdd)} style={{
          padding: '0.45rem 1.1rem', borderRadius: 8, border: 'none', cursor: 'pointer',
          background: showAdd ? '#333' : '#f97316', color: '#fff', fontSize: '0.82rem', fontWeight: 600,
        }}>{showAdd ? 'Cancel' : '+ New Memory'}</button>
      </div>

      {/* Add memory */}
      {showAdd && (
        <div style={{ ...card, marginBottom: '1rem', borderColor: '#f97316' }}>
          <div style={{ display: 'flex', gap: '0.5rem' }}>
            <input
              style={{ ...input, flex: 1 }} placeholder="Add a memory..."
              value={addText} onChange={e => setAddText(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter') doAdd(); }}
              autoFocus
            />
            <button onClick={doAdd} disabled={adding} style={{
              padding: '0.5rem 1.25rem', borderRadius: 6, border: 'none', cursor: 'pointer',
              background: adding ? '#333' : '#f97316', color: '#fff', fontSize: '0.85rem', fontWeight: 600,
            }}>{adding ? 'Adding...' : 'Add'}</button>
          </div>
        </div>
      )}

      {/* Search bar */}
      <div style={{ ...card, marginBottom: '1rem', padding: '0.75rem 1rem' }}>
        <div style={{ display: 'flex', gap: '0.5rem' }}>
          <input
            style={{ ...input, flex: 1, border: '1px solid #333' }}
            placeholder="Semantic search memories..."
            value={searchQuery}
            onChange={e => setSearchQuery(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') doSearch(); }}
          />
          <button onClick={doSearch} disabled={searching} style={{
            padding: '0.5rem 1rem', borderRadius: 6, border: 'none', cursor: 'pointer',
            background: '#8b5cf6', color: '#fff', fontSize: '0.82rem', fontWeight: 600,
          }}>{searching ? '...' : 'Search'}</button>
          {searchResults.length > 0 && (
            <button onClick={() => { setSearchResults([]); setSearchQuery(''); }} style={{
              padding: '0.5rem 0.75rem', borderRadius: 6, border: '1px solid #333',
              background: 'transparent', color: '#888', cursor: 'pointer', fontSize: '0.82rem',
            }}>Clear</button>
          )}
        </div>
      </div>

      {/* Main content: memory list + detail */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '1rem' }}>
        {/* Memory list */}
        <div style={card}>
          <SectionHeader title={searchResults.length > 0 ? `Search Results (${searchResults.length})` : `Memories (${memories.length})`} />
          <div style={{ maxHeight: 'calc(100vh - 380px)', overflow: 'auto', display: 'flex', flexDirection: 'column', gap: '0.3rem' }}>
            {displayList.length === 0 && (
              <div style={{ padding: '2rem', textAlign: 'center', color: '#555' }}>No memories stored yet</div>
            )}
            {displayList.map(m => (
              <div
                key={m.id}
                onClick={() => setSelected(m)}
                style={{
                  padding: '0.65rem 0.75rem', borderRadius: 8, cursor: 'pointer',
                  background: selected?.id === m.id ? '#1a1a2e' : '#0a0a0a',
                  border: `1px solid ${selected?.id === m.id ? '#2563eb44' : '#111'}`,
                  transition: 'background 0.15s',
                }}
              >
                <div style={{
                  fontSize: '0.82rem', color: selected?.id === m.id ? '#fff' : '#ccc',
                  overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                }}>{m.memory}</div>
                {m.created_at && (
                  <div style={{ fontSize: '0.65rem', color: '#444', marginTop: '0.2rem' }}>
                    {m.id.slice(0, 8)} · {new Date(m.created_at).toLocaleDateString()}
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>

        {/* Detail / Version History */}
        <div style={card}>
          <SectionHeader title="Version History" />
          {selected ? (
            <div>
              <div style={{
                background: '#0a0a0a', borderRadius: 8, padding: '1rem', marginBottom: '1rem',
                border: '1px solid #1a1a1a',
              }}>
                <div style={{ fontSize: '0.9rem', color: '#e0e0e0', lineHeight: 1.6, whiteSpace: 'pre-wrap' }}>{selected.memory}</div>
              </div>
              {/* Version timeline */}
              <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
                {selected.updated_at && selected.updated_at !== selected.created_at && (
                  <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                    <div style={{ width: 8, height: 8, borderRadius: '50%', background: '#4ade80', flexShrink: 0 }} />
                    <div style={{ flex: 1, fontSize: '0.78rem' }}>
                      <span style={{ color: '#4ade80', fontWeight: 600 }}>Current</span>
                      <span style={{ color: '#555', marginLeft: '0.5rem' }}>{new Date(selected.updated_at).toLocaleString()}</span>
                    </div>
                  </div>
                )}
                {selected.created_at && (
                  <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                    <div style={{ width: 8, height: 8, borderRadius: '50%', background: '#333', flexShrink: 0 }} />
                    <div style={{ flex: 1, fontSize: '0.78rem' }}>
                      <span style={{ color: '#888' }}>Created</span>
                      <span style={{ color: '#555', marginLeft: '0.5rem' }}>{new Date(selected.created_at).toLocaleString()}</span>
                    </div>
                  </div>
                )}
              </div>
              {/* Metadata */}
              <div style={{ marginTop: '1rem', padding: '0.75rem', background: '#0a0a0a', borderRadius: 6, fontSize: '0.72rem', fontFamily: 'monospace', color: '#555' }}>
                ID: {selected.id}
              </div>
            </div>
          ) : (
            <div style={{ padding: '3rem', textAlign: 'center', color: '#555' }}>
              Select a memory to view details
            </div>
          )}
        </div>
      </div>

      {/* Status bar */}
      {status && (
        <div style={{
          marginTop: '1rem', display: 'flex', gap: '1.5rem', padding: '0.6rem 1rem',
          background: '#0a0a0a', borderRadius: 8, border: '1px solid #111', fontSize: '0.72rem', color: '#555',
        }}>
          <span style={{ display: 'flex', alignItems: 'center', gap: '0.3rem' }}>
            <span style={{ width: 5, height: 5, borderRadius: '50%', background: status.status === 'ok' ? '#4ade80' : '#ef4444' }} />
            {status.status === 'ok' ? 'Connected' : 'Error'}
          </span>
          <span>{status.total_memories} memories</span>
          <span>Qdrant: {status.qdrant}</span>
        </div>
      )}

      {/* Toast */}
      {toast && (
        <div style={{
          position: 'fixed', bottom: '1.5rem', right: '1.5rem',
          background: '#1a1a2e', color: '#fff', padding: '0.75rem 1.5rem',
          borderRadius: '8px', fontSize: '0.85rem', zIndex: 9999,
          boxShadow: '0 4px 20px rgba(0,0,0,0.4)', border: '1px solid #2563eb33',
        }}>{toast}</div>
      )}
    </div>
  );
}
