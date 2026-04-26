import { useEffect, useRef, useState } from 'react';
import { StatusBadge } from '../components/Charts';

export default function LogsPage() {
  const [logs, setLogs] = useState<string[]>([]);
  const [connected, setConnected] = useState(false);
  const [autoScroll, setAutoScroll] = useState(true);
  const [filter, setFilter] = useState('');
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const wsUrl = `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}/ws/logs`;
    const ws = new WebSocket(wsUrl);

    ws.onopen = () => setConnected(true);
    ws.onclose = () => setConnected(false);
    ws.onmessage = (e) => {
      setLogs(prev => {
        const next = [...prev, e.data];
        return next.length > 500 ? next.slice(-500) : next;
      });
    };

    return () => ws.close();
  }, []);

  useEffect(() => {
    if (autoScroll) bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [logs, autoScroll]);

  const filtered = filter
    ? logs.filter(l => l.toLowerCase().includes(filter.toLowerCase()))
    : logs;

  const errorCount = logs.filter(l => l.includes('ERROR')).length;
  const warnCount = logs.filter(l => l.includes('WARNING')).length;

  const getLineColor = (line: string) => {
    if (line.includes('ERROR')) return '#ef4444';
    if (line.includes('WARNING')) return '#f59e0b';
    if (line.includes('INFO')) return '#3b82f6';
    if (line.includes('DEBUG')) return '#555';
    return '#777';
  };

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1rem' }}>
        <h1 style={{ fontSize: '1.5rem', fontWeight: 600 }}>Live Logs</h1>
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
          {/* Stats */}
          <div style={{ display: 'flex', gap: '0.5rem', fontSize: '0.72rem' }}>
            <span style={{ color: '#888' }}>{logs.length} lines</span>
            {errorCount > 0 && <span style={{ color: '#ef4444' }}>{errorCount} errors</span>}
            {warnCount > 0 && <span style={{ color: '#f59e0b' }}>{warnCount} warnings</span>}
          </div>
          <StatusBadge status={connected ? 'running' : 'error'} label={connected ? 'Connected' : 'Disconnected'} />
        </div>
      </div>

      {/* Toolbar */}
      <div style={{
        display: 'flex', gap: '0.5rem', marginBottom: '0.75rem', alignItems: 'center',
      }}>
        <input
          value={filter}
          onChange={e => setFilter(e.target.value)}
          placeholder="Filter logs..."
          style={{
            flex: 1, padding: '0.45rem 0.75rem', borderRadius: 8,
            border: '1px solid #1a1a1a', background: '#111', color: '#e0e0e0',
            fontSize: '0.82rem', outline: 'none', fontFamily: 'monospace',
          }}
        />
        <button
          onClick={() => setAutoScroll(!autoScroll)}
          style={{
            padding: '0.45rem 0.85rem', borderRadius: 8, border: `1px solid ${autoScroll ? '#f9731644' : '#1a1a1a'}`,
            background: autoScroll ? '#f9731622' : '#111', color: autoScroll ? '#f97316' : '#666',
            cursor: 'pointer', fontSize: '0.78rem', fontWeight: 500,
          }}
        >{autoScroll ? 'Auto-scroll ON' : 'Auto-scroll OFF'}</button>
        <button
          onClick={() => setLogs([])}
          style={{
            padding: '0.45rem 0.85rem', borderRadius: 8, border: '1px solid #1a1a1a',
            background: '#111', color: '#666', cursor: 'pointer', fontSize: '0.78rem',
          }}
        >Clear</button>
      </div>

      {/* Log output */}
      <div style={{
        background: '#0a0a0a', border: '1px solid #1a1a1a', borderRadius: '12px',
        padding: '0.75rem 1rem', height: 'calc(100vh - 200px)', overflow: 'auto',
        fontFamily: '"JetBrains Mono", "Fira Code", "SF Mono", monospace',
        fontSize: '0.78rem', lineHeight: '1.7',
      }}>
        {filtered.length === 0 && (
          <div style={{ color: '#333', padding: '2rem', textAlign: 'center' }}>
            {logs.length === 0 ? 'Waiting for logs...' : 'No matching logs'}
          </div>
        )}
        {filtered.map((line, i) => (
          <div key={i} style={{
            color: getLineColor(line),
            padding: '0 0.25rem',
            borderLeft: line.includes('ERROR') ? '2px solid #ef4444' : line.includes('WARNING') ? '2px solid #f59e0b' : '2px solid transparent',
            marginLeft: -4,
            paddingLeft: '0.5rem',
          }}>{line}</div>
        ))}
        <div ref={bottomRef} />
      </div>
    </div>
  );
}
