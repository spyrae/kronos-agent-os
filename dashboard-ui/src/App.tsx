import { useState, useEffect } from 'react';
import { BrowserRouter, Routes, Route, NavLink } from 'react-router-dom';
import { getToken, setToken, api } from './api/client';
import OverviewPage from './pages/OverviewPage';
import PersonaPage from './pages/PersonaPage';
import LogsPage from './pages/LogsPage';
import McpPage from './pages/McpPage';
import SkillsPage from './pages/SkillsPage';
import AgentsPage from './pages/AgentsPage';
import GraphPage from './pages/GraphPage';
import SwarmPage from './pages/SwarmPage';
import ConfigPage from './pages/ConfigPage';
import MemoryPage from './pages/MemoryPage';
import PerformancePage from './pages/PerformancePage';
import AnalyticsPage from './pages/AnalyticsPage';
import AuditTrailPage from './pages/AuditTrailPage';
import AnomaliesPage from './pages/AnomaliesPage';

/* ── Login ── */

function Login() {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  const submit = async () => {
    if (!username || !password) { setError('Enter username and password'); return; }
    setLoading(true);
    setError('');
    try {
      const res = await fetch(`${import.meta.env.VITE_API_URL || ''}/api/auth/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      });
      if (!res.ok) { setError('Invalid credentials'); setLoading(false); return; }
      const data = await res.json();
      setToken(data.token);
      window.location.reload();
    } catch {
      setError('Connection failed');
      setLoading(false);
    }
  };

  const inputStyle: React.CSSProperties = {
    padding: '0.85rem 1.25rem', fontSize: '1rem', borderRadius: '10px',
    border: `1px solid ${error ? '#ef4444' : '#222'}`, background: '#111', color: '#fff', width: '340px',
    outline: 'none', marginBottom: '0.75rem', display: 'block',
  };

  return (
    <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', height: '100vh', background: '#09090b' }}>
      <div style={{ textAlign: 'center', color: '#e0e0e0' }}>
        {/* Logo */}
        <div style={{
          width: 56, height: 56, borderRadius: '14px', background: '#f97316',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          margin: '0 auto 1rem', fontSize: '1.5rem', color: '#fff', fontWeight: 700,
        }}>K</div>
        <div style={{ fontSize: '1.75rem', fontWeight: 600, marginBottom: '0.25rem' }}>Kronos Agent OS</div>
        <div style={{ fontSize: '0.85rem', color: '#555', marginBottom: '2.5rem' }}>Agent Operations Dashboard</div>
        <input
          type="text" placeholder="Username" value={username}
          onChange={e => setUsername(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') document.getElementById('pwd')?.focus(); }}
          autoFocus autoComplete="username"
          style={inputStyle}
        />
        <input
          id="pwd" type="password" placeholder="Password" value={password}
          onChange={e => setPassword(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') submit(); }}
          autoComplete="current-password"
          style={inputStyle}
        />
        <button onClick={submit} disabled={loading} style={{
          marginTop: '0.5rem', padding: '0.75rem 2.5rem', borderRadius: '10px', width: '340px',
          background: loading ? '#555' : '#f97316', color: '#fff', border: 'none',
          cursor: loading ? 'wait' : 'pointer', fontSize: '1rem', fontWeight: 600,
        }}>{loading ? 'Signing in...' : 'Sign In'}</button>
        {error && <div style={{ marginTop: '0.75rem', color: '#ef4444', fontSize: '0.85rem' }}>{error}</div>}
      </div>
    </div>
  );
}

/* ── Navigation structure ── */

interface NavSection {
  title: string;
  items: { path: string; label: string; icon: string }[];
}

const NAV_SECTIONS: NavSection[] = [
  {
    title: 'MONITORING',
    items: [
      { path: '/', label: 'Overview', icon: '~' },
      { path: '/agents', label: 'Agents', icon: 'A' },
      { path: '/memory', label: 'Memory Explorer', icon: 'M' },
      { path: '/logs', label: 'Live Logs', icon: 'L' },
    ],
  },
  {
    title: 'OPERATIONS',
    items: [
      { path: '/performance', label: 'Performance', icon: 'P' },
      { path: '/analytics', label: 'Analytics', icon: 'N' },
      { path: '/audit', label: 'Audit Trail', icon: 'T' },
    ],
  },
  {
    title: 'MANAGEMENT',
    items: [
      { path: '/anomalies', label: 'Anomalies', icon: '!' },
      { path: '/mcp', label: 'MCP Servers', icon: 'S' },
      { path: '/skills', label: 'Skills', icon: 'K' },
      { path: '/graph', label: 'Graph', icon: 'G' },
      { path: '/swarm', label: 'Swarm', icon: 'Q' },
      { path: '/persona', label: 'Persona', icon: 'W' },
      { path: '/config', label: 'Settings', icon: 'C' },
    ],
  },
];

const ICON_COLORS: Record<string, string> = {
  '~': '#f97316', A: '#3b82f6', M: '#8b5cf6', L: '#4ade80',
  P: '#f59e0b', N: '#06b6d4', T: '#ec4899',
  '!': '#ef4444', S: '#6366f1', K: '#14b8a6', G: '#a78bfa', Q: '#22c55e', W: '#f472b6', C: '#64748b',
};

/* ── Layout with grouped sidebar ── */

function Layout({ children }: { children: React.ReactNode }) {
  const [uptime, setUptime] = useState(0);
  const [searchOpen, setSearchOpen] = useState(false);
  const [search, setSearch] = useState('');

  useEffect(() => {
    const fetchUptime = () => api<{ uptime_seconds: number }>('/api/health')
      .then(r => setUptime(r.uptime_seconds)).catch(() => {});
    fetchUptime();
    const interval = setInterval(fetchUptime, 30000);
    return () => clearInterval(interval);
  }, []);

  // Keyboard shortcut: Cmd+K to toggle search
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
        e.preventDefault();
        setSearchOpen(v => !v);
      }
      if (e.key === 'Escape') setSearchOpen(false);
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, []);

  const formatUptime = (s: number) => {
    if (s < 3600) return `${Math.floor(s / 60)}m`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
    return `${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h`;
  };

  const filteredSections = search
    ? NAV_SECTIONS.map(s => ({
        ...s,
        items: s.items.filter(i => i.label.toLowerCase().includes(search.toLowerCase())),
      })).filter(s => s.items.length > 0)
    : NAV_SECTIONS;

  return (
    <div style={{
      display: 'flex', minHeight: '100vh', background: '#09090b', color: '#e0e0e0',
      fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", "Inter", sans-serif',
    }}>
      {/* Sidebar */}
      <nav style={{
        width: '220px', padding: '1rem 0.65rem', borderRight: '1px solid #1a1a1a',
        display: 'flex', flexDirection: 'column', background: '#0c0c0e',
        position: 'fixed', top: 0, left: 0, bottom: 0, overflow: 'auto',
      }}>
        {/* Logo + Status */}
        <div style={{ padding: '0.25rem 0.5rem', marginBottom: '1rem' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.6rem' }}>
            <div style={{
              width: 32, height: 32, borderRadius: '8px', background: '#f97316',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              fontSize: '0.85rem', color: '#fff', fontWeight: 700,
            }}>K</div>
            <div>
              <div style={{ fontSize: '1rem', fontWeight: 600, color: '#fff', letterSpacing: '-0.02em' }}>Kronos Agent OS</div>
              <div style={{ fontSize: '0.65rem', color: '#4ade80', display: 'flex', alignItems: 'center', gap: '0.3rem' }}>
                <span style={{ width: 5, height: 5, borderRadius: '50%', background: '#4ade80', display: 'inline-block' }} />
                Online {uptime > 0 ? `(${formatUptime(uptime)})` : ''}
              </div>
            </div>
          </div>
        </div>

        {/* Search */}
        <div
          onClick={() => setSearchOpen(true)}
          style={{
            padding: '0.4rem 0.6rem', borderRadius: '6px', border: '1px solid #1a1a1a',
            background: '#111', marginBottom: '1rem', cursor: 'pointer',
            display: 'flex', alignItems: 'center', justifyContent: 'space-between',
          }}
        >
          <span style={{ fontSize: '0.8rem', color: '#555' }}>Search...</span>
          <span style={{ fontSize: '0.65rem', color: '#444', background: '#1a1a1a', padding: '0.1rem 0.35rem', borderRadius: '3px' }}>⌘K</span>
        </div>

        {searchOpen && (
          <div style={{ marginBottom: '0.5rem', position: 'relative' }}>
            <input
              value={search}
              onChange={e => setSearch(e.target.value)}
              placeholder="Search pages..."
              autoFocus
              onBlur={() => { if (!search) setSearchOpen(false); }}
              style={{
                width: '100%', padding: '0.45rem 0.6rem', borderRadius: '6px',
                border: '1px solid #f97316', background: '#111', color: '#fff',
                fontSize: '0.8rem', outline: 'none',
              }}
            />
          </div>
        )}

        {/* Nav sections */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: '1.25rem', flex: 1 }}>
          {filteredSections.map(section => (
            <div key={section.title}>
              <div style={{
                fontSize: '0.65rem', fontWeight: 600, color: '#444', letterSpacing: '0.08em',
                padding: '0 0.5rem', marginBottom: '0.4rem',
              }}>{section.title}</div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: '1px' }}>
                {section.items.map(item => (
                  <NavLink
                    key={item.path}
                    to={item.path}
                    end={item.path === '/'}
                    onClick={() => { setSearch(''); setSearchOpen(false); }}
                    style={({ isActive }) => ({
                      padding: '0.45rem 0.6rem',
                      borderRadius: '6px',
                      textDecoration: 'none',
                      color: isActive ? '#fff' : '#777',
                      background: isActive ? '#1a1a2e' : 'transparent',
                      display: 'flex',
                      alignItems: 'center',
                      gap: '0.55rem',
                      fontSize: '0.82rem',
                      fontWeight: isActive ? 500 : 400,
                    })}
                  >
                    <span style={{
                      width: '20px', height: '20px', borderRadius: '4px',
                      background: '#111', display: 'flex', alignItems: 'center', justifyContent: 'center',
                      fontSize: '0.6rem', fontWeight: 700, color: ICON_COLORS[item.icon] || '#6366f1',
                      border: '1px solid #1a1a1a',
                    }}>{item.icon}</span>
                    {item.label}
                  </NavLink>
                ))}
              </div>
            </div>
          ))}
        </div>

        {/* Footer */}
        <div style={{ padding: '0.75rem 0.5rem', borderTop: '1px solid #1a1a1a', marginTop: '0.5rem' }}>
          <div style={{ fontSize: '0.65rem', color: '#333', marginBottom: '0.5rem' }}>v0.1.0 · Kronos Agent OS</div>
          <button
            onClick={() => { localStorage.removeItem('kronos_token'); window.location.reload(); }}
            style={{
              background: 'none', border: '1px solid #1a1a1a', borderRadius: '6px',
              color: '#555', cursor: 'pointer', fontSize: '0.75rem', padding: '0.35rem 0.75rem',
              width: '100%',
            }}
          >Logout</button>
        </div>
      </nav>

      {/* Main content */}
      <main style={{ flex: 1, marginLeft: '220px', padding: '1.5rem 2rem', overflow: 'auto', minHeight: '100vh' }}>
        {children}
      </main>
    </div>
  );
}

/* ── App ── */

export default function App() {
  if (!getToken()) return <Login />;
  return (
    <BrowserRouter>
      <Layout>
        <Routes>
          <Route path="/" element={<OverviewPage />} />
          <Route path="/agents" element={<AgentsPage />} />
          <Route path="/memory" element={<MemoryPage />} />
          <Route path="/logs" element={<LogsPage />} />
          <Route path="/performance" element={<PerformancePage />} />
          <Route path="/analytics" element={<AnalyticsPage />} />
          <Route path="/audit" element={<AuditTrailPage />} />
          <Route path="/anomalies" element={<AnomaliesPage />} />
          <Route path="/mcp" element={<McpPage />} />
          <Route path="/skills" element={<SkillsPage />} />
          <Route path="/graph" element={<GraphPage />} />
          <Route path="/swarm" element={<SwarmPage />} />
          <Route path="/persona" element={<PersonaPage />} />
          <Route path="/config" element={<ConfigPage />} />
        </Routes>
      </Layout>
    </BrowserRouter>
  );
}
