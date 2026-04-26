import { useEffect, useState } from 'react';
import { api } from '../api/client';
import { SectionHeader, StatusBadge } from '../components/Charts';

interface McpServer {
  name: string; transport: string; command: string; args: string[];
  source: string; disabled: boolean;
}

export default function McpPage() {
  const [servers, setServers] = useState<McpServer[]>([]);
  const [showAdd, setShowAdd] = useState(false);
  const [newName, setNewName] = useState('');
  const [newCommand, setNewCommand] = useState('npx');
  const [newArgs, setNewArgs] = useState('');
  const [newEnv, setNewEnv] = useState('');
  const [adding, setAdding] = useState(false);
  const [toast, setToast] = useState('');
  const [error, setError] = useState('');

  const showToast = (msg: string) => { setToast(msg); setTimeout(() => setToast(''), 3000); };
  const showError = (msg: string) => { setError(msg); setTimeout(() => setError(''), 5000); };

  const load = async () => {
    try {
      const r = await api<{ servers: McpServer[] }>('/api/mcp/servers');
      setServers(r.servers);
    } catch (e: any) { showError(e.message); }
  };
  useEffect(() => { load(); }, []);

  const toggle = async (name: string) => {
    try {
      const r = await api<{ disabled: boolean }>(`/api/mcp/servers/${name}/toggle`, { method: 'POST' });
      showToast(`${name} ${r.disabled ? 'disabled' : 'enabled'}`);
      load();
    } catch (e: any) { showError(e.message); }
  };

  const remove = async (name: string) => {
    if (!confirm(`Delete server "${name}"?`)) return;
    try {
      await api(`/api/mcp/servers/${name}`, { method: 'DELETE' });
      showToast(`${name} deleted`);
      load();
    } catch (e: any) { showError(e.message); }
  };

  const addServer = async () => {
    const name = newName.trim();
    if (!name || !newCommand.trim()) { showError('Name and command are required'); return; }
    setAdding(true);
    try {
      const envObj: Record<string, string> = {};
      newEnv.split('\n').filter(Boolean).forEach(line => {
        const [k, ...v] = line.split('=');
        if (k) envObj[k.trim()] = v.join('=').trim();
      });
      await api('/api/mcp/servers', {
        method: 'POST',
        body: JSON.stringify({ name, command: newCommand.trim(), args: newArgs.split(' ').filter(Boolean), env: envObj }),
      });
      showToast(`Server "${name}" added`);
      setShowAdd(false);
      setNewName(''); setNewArgs(''); setNewEnv(''); setNewCommand('npx');
      load();
    } catch (e: any) { showError(e.message); }
    setAdding(false);
  };

  const card: React.CSSProperties = {
    background: '#111', border: '1px solid #1a1a1a', borderRadius: '12px', padding: '1.25rem',
  };
  const input: React.CSSProperties = {
    padding: '0.55rem 0.85rem', borderRadius: 8, border: '1px solid #222',
    background: '#0a0a0a', color: '#fff', width: '100%', fontSize: '0.85rem',
    outline: 'none', marginBottom: '0.5rem',
  };

  const active = servers.filter(s => !s.disabled).length;

  return (
    <div>
      {/* Toast / Error */}
      {toast && <div style={{ position: 'fixed', top: '1rem', right: '1rem', padding: '0.75rem 1.5rem', borderRadius: 8, background: '#166534', color: '#fff', fontSize: '0.85rem', zIndex: 100, boxShadow: '0 4px 20px rgba(0,0,0,0.4)', border: '1px solid #4ade8033' }}>{toast}</div>}
      {error && <div style={{ position: 'fixed', top: '1rem', right: '1rem', padding: '0.75rem 1.5rem', borderRadius: 8, background: '#991b1b', color: '#fff', fontSize: '0.85rem', zIndex: 100, boxShadow: '0 4px 20px rgba(0,0,0,0.4)', border: '1px solid #ef444433' }}>{error}</div>}

      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1.25rem' }}>
        <div>
          <h1 style={{ fontSize: '1.5rem', fontWeight: 600 }}>MCP Servers ({servers.length})</h1>
          <p style={{ fontSize: '0.78rem', color: '#555', marginTop: '0.2rem' }}>{active} active · {servers.length - active} disabled</p>
        </div>
        <button onClick={() => setShowAdd(!showAdd)} style={{
          padding: '0.45rem 1.1rem', borderRadius: 8, border: 'none', cursor: 'pointer',
          background: showAdd ? '#333' : '#f97316', color: '#fff', fontSize: '0.82rem', fontWeight: 600,
        }}>{showAdd ? 'Cancel' : '+ Add Server'}</button>
      </div>

      {/* Add form */}
      {showAdd && (
        <div style={{ ...card, marginBottom: '1rem', borderColor: '#f97316' }}>
          <SectionHeader title="New MCP Server (stdio)" />
          <input placeholder="Server name" value={newName} onChange={e => setNewName(e.target.value)} autoFocus style={input} />
          <input placeholder="Command (npx, uvx, node)" value={newCommand} onChange={e => setNewCommand(e.target.value)} style={input} />
          <input placeholder="Args (e.g. -y @scope/mcp-server)" value={newArgs} onChange={e => setNewArgs(e.target.value)} style={input} />
          <textarea
            placeholder={'Env vars, one per line:\nAPI_KEY=your-key'}
            value={newEnv} onChange={e => setNewEnv(e.target.value)} rows={3}
            style={{ ...input, resize: 'vertical', fontFamily: 'monospace' }}
          />
          <button onClick={addServer} disabled={adding} style={{
            padding: '0.55rem 1.5rem', borderRadius: 8, border: 'none', cursor: adding ? 'wait' : 'pointer',
            background: adding ? '#333' : '#f97316', color: '#fff', fontSize: '0.85rem', fontWeight: 600,
          }}>{adding ? 'Adding...' : 'Add Server'}</button>
        </div>
      )}

      {/* Server grid */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(340px, 1fr))', gap: '1rem' }}>
        {servers.map(s => (
          <div key={s.name} style={{
            ...card,
            borderLeft: `3px solid ${s.disabled ? '#333' : '#6366f1'}`,
            opacity: s.disabled ? 0.55 : 1,
          }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '0.6rem' }}>
              <span style={{ fontWeight: 600, fontSize: '0.95rem', color: s.disabled ? '#666' : '#fff' }}>{s.name}</span>
              <div style={{ display: 'flex', gap: '0.3rem' }}>
                <button onClick={() => toggle(s.name)} style={{
                  padding: '0.2rem 0.65rem', borderRadius: 4, border: 'none', cursor: 'pointer', fontSize: '0.72rem', fontWeight: 600,
                  background: s.disabled ? '#1a2e1a' : '#7f1d1d', color: '#fff',
                }}>{s.disabled ? 'Enable' : 'Disable'}</button>
                {s.source === 'custom' && (
                  <button onClick={() => remove(s.name)} style={{
                    padding: '0.2rem 0.5rem', borderRadius: 4, border: 'none', cursor: 'pointer', fontSize: '0.72rem',
                    background: '#1a1a1a', color: '#ef4444',
                  }}>Delete</button>
                )}
              </div>
            </div>
            <div style={{ fontSize: '0.78rem', color: '#888', fontFamily: 'monospace', marginBottom: '0.6rem', wordBreak: 'break-all' }}>
              {s.command} {s.args.join(' ')}
            </div>
            <div style={{ display: 'flex', gap: '0.3rem' }}>
              <span style={{
                padding: '0.12rem 0.45rem', borderRadius: 4, fontSize: '0.62rem', fontWeight: 600,
                background: s.source === 'builtin' ? '#1e3a5f' : '#3a1e5f',
                color: s.source === 'builtin' ? '#93c5fd' : '#c4b5fd',
              }}>{s.source}</span>
              <StatusBadge status={s.disabled ? 'stopped' : 'running'} label={s.disabled ? 'disabled' : 'active'} />
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
