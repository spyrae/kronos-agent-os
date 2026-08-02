import { useEffect, useState } from 'react';
import { api } from '../api/client';

/**
 * Where the agent may act as the owner, and what it may do there.
 *
 * A password entered here goes one way: it is sent, stored encrypted, and never
 * comes back — there is no endpoint that returns one, so the form cannot show a
 * stored password even to the person who typed it. Everything on this page is
 * therefore either a setting or a yes/no about whether a secret exists.
 */

interface Account {
  site: string;
  domains: string[];
  login: string;
  login_url: string;
  has_profile: boolean;
  has_password: boolean;
  permission: string;
  approval_required: boolean;
  session_state: string;
  last_used_at: number | null;
  notes: string;
}

interface AccountsResponse {
  accounts: Account[];
  permissions: string[];
  actions: string[];
  password_vault_enabled: boolean;
  vault_key_source: string;
  vault_hint: string;
}

const PERMISSION_MEANING: Record<string, string> = {
  read: 'search and read pages while signed in — nothing else',
  message: 'also write to hosts and sellers',
  full: 'also book, order and pay',
};

const SESSION_COLOR: Record<string, string> = {
  ok: '#22c55e',
  expired: '#f59e0b',
  unknown: '#64748b',
};

const EMPTY = {
  site: '',
  domains: '',
  login: '',
  login_url: '',
  profile_dir: '',
  permission: 'read',
  approval_required: true,
  notes: '',
  password: '',
};

function ago(ts: number | null): string {
  if (!ts) return 'never used';
  const mins = Math.floor((Date.now() / 1000 - ts) / 60);
  if (mins < 60) return `${mins}m ago`;
  if (mins < 1440) return `${Math.floor(mins / 60)}h ago`;
  return `${Math.floor(mins / 1440)}d ago`;
}

const card: React.CSSProperties = {
  background: '#111', border: '1px solid #1a1a1a', borderRadius: 12, padding: '1.1rem',
};
const input: React.CSSProperties = {
  padding: '0.5rem 0.7rem', borderRadius: 8, border: '1px solid #222', background: '#0c0c0c',
  color: '#e0e0e0', fontSize: '0.85rem', width: '100%', outline: 'none',
};
const label: React.CSSProperties = { fontSize: '0.7rem', color: '#666', display: 'block', marginBottom: '0.25rem' };
const button = (bg: string): React.CSSProperties => ({
  padding: '0.45rem 0.9rem', borderRadius: 8, border: 'none', cursor: 'pointer',
  background: bg, color: '#fff', fontSize: '0.8rem', fontWeight: 600,
});
const ghost: React.CSSProperties = {
  padding: '0.4rem 0.8rem', borderRadius: 8, border: '1px solid #222', cursor: 'pointer',
  background: 'transparent', color: '#999', fontSize: '0.78rem',
};

export default function AccountsPage() {
  const [data, setData] = useState<AccountsResponse | null>(null);
  const [form, setForm] = useState({ ...EMPTY });
  const [editing, setEditing] = useState<string | null>(null);
  const [passwordFor, setPasswordFor] = useState<string | null>(null);
  const [newPassword, setNewPassword] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const load = () => api<AccountsResponse>('/api/accounts/').then(setData).catch(e => setError(String(e)));

  useEffect(() => { load(); }, []);

  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError('');
    try {
      await fn();
      await load();
      return true;
    } catch (e) {
      setError(e instanceof Error ? e.message.replace(/^\d+:\s*/, '') : String(e));
      return false;
    } finally {
      setBusy(false);
    }
  };

  const save = async () => {
    const ok = await run(() => api('/api/accounts/', {
      method: 'POST',
      body: JSON.stringify({
        site: form.site,
        domains: form.domains.split(',').map(d => d.trim()).filter(Boolean),
        login: form.login,
        login_url: form.login_url,
        profile_dir: form.profile_dir,
        permission: form.permission,
        approval_required: form.approval_required,
        notes: form.notes,
        password: form.password,
      }),
    }));
    if (ok) { setForm({ ...EMPTY }); setEditing(null); }
  };

  const edit = (a: Account) => {
    setEditing(a.site);
    // profile_dir is never returned by the API. Leaving it blank re-derives it
    // for an account with a password, and the field is there for the rest.
    setForm({
      site: a.site, domains: a.domains.join(', '), login: a.login, login_url: a.login_url,
      profile_dir: '', permission: a.permission, approval_required: a.approval_required,
      notes: a.notes, password: '',
    });
  };

  const storePassword = async (site: string) => {
    const ok = await run(() => api(`/api/accounts/${site}/password`, {
      method: 'PUT', body: JSON.stringify({ password: newPassword }),
    }));
    if (ok) { setNewPassword(''); setPasswordFor(null); }
  };

  const vaultOff = data && !data.password_vault_enabled;

  return (
    <div>
      <div style={{ marginBottom: '1.25rem' }}>
        <h1 style={{ fontSize: '1.5rem', fontWeight: 600, marginBottom: '0.2rem' }}>Site Accounts</h1>
        <p style={{ fontSize: '0.82rem', color: '#666' }}>
          Where the agent may act as you, and what it may do there. Passwords are stored encrypted and
          never shown again — not here, and never to the agent itself.
        </p>
      </div>

      {error && (
        <div style={{ ...card, borderColor: '#ef444455', color: '#fca5a5', marginBottom: '1rem', fontSize: '0.85rem' }}>
          {error}
        </div>
      )}

      {data && (
        <div style={{
          ...card, marginBottom: '1rem', fontSize: '0.8rem',
          borderColor: vaultOff ? '#f59e0b55' : '#1a1a1a', color: vaultOff ? '#fcd34d' : '#666',
        }}>
          {vaultOff
            ? `Password storage is unavailable: ${data.vault_hint}. Accounts still work with a browser profile you sign into by hand.`
            : `Vault key: ${data.vault_key_source === 'env' ? 'environment (VAULT_KEY)' : 'key file on this host'}.`}
        </div>
      )}

      <div style={{ display: 'grid', gap: '0.75rem', marginBottom: '1.5rem' }}>
        {data?.accounts.length === 0 && (
          <div style={{ ...card, color: '#666', fontSize: '0.85rem' }}>
            No accounts yet. Add one below, then sign into that browser profile once by hand.
          </div>
        )}

        {data?.accounts.map(a => (
          <div key={a.site} style={card}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: '1rem' }}>
              <div style={{ minWidth: 0 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginBottom: '0.3rem' }}>
                  <span style={{ fontSize: '1rem', fontWeight: 600 }}>{a.site}</span>
                  <span style={{
                    fontSize: '0.68rem', padding: '0.15rem 0.5rem', borderRadius: 20,
                    background: '#f9731622', color: '#f97316', fontWeight: 600,
                  }}>may {a.permission}</span>
                  <span style={{ fontSize: '0.68rem', color: SESSION_COLOR[a.session_state] || '#64748b' }}>
                    ● session {a.session_state}
                  </span>
                </div>
                <div style={{ fontSize: '0.78rem', color: '#777', marginBottom: '0.15rem' }}>
                  {a.login || 'no login set'} · {a.domains.join(', ')}
                </div>
                <div style={{ fontSize: '0.72rem', color: '#555' }}>
                  {PERMISSION_MEANING[a.permission]} · {a.approval_required ? 'asks before anything that leaves a trace' : 'acts without asking'} · {ago(a.last_used_at)}
                </div>
              </div>
              <div style={{ display: 'flex', gap: '0.4rem', flexShrink: 0 }}>
                <span style={{
                  fontSize: '0.7rem', padding: '0.3rem 0.6rem', borderRadius: 6, alignSelf: 'center',
                  background: a.has_password ? '#22c55e18' : '#1a1a1a',
                  color: a.has_password ? '#4ade80' : '#666',
                }}>{a.has_password ? 'password stored' : 'no password'}</span>
                <button style={ghost} onClick={() => edit(a)}>Edit</button>
                <button
                  style={ghost}
                  disabled={vaultOff || busy}
                  onClick={() => { setPasswordFor(passwordFor === a.site ? null : a.site); setNewPassword(''); }}
                >{a.has_password ? 'Change password' : 'Add password'}</button>
                {a.has_password && (
                  <button
                    style={ghost}
                    disabled={busy}
                    onClick={() => run(() => api(`/api/accounts/${a.site}/password`, { method: 'DELETE' }))}
                  >Forget password</button>
                )}
                <button
                  style={{ ...ghost, color: '#ef4444' }}
                  disabled={busy}
                  onClick={() => { if (confirm(`Remove the account for ${a.site}?`)) run(() => api(`/api/accounts/${a.site}`, { method: 'DELETE' })); }}
                >Remove</button>
              </div>
            </div>

            {passwordFor === a.site && (
              <div style={{ marginTop: '0.9rem', paddingTop: '0.9rem', borderTop: '1px solid #1a1a1a' }}>
                <label style={label}>Password for {a.login || a.site} — stored encrypted, never displayed again</label>
                <div style={{ display: 'flex', gap: '0.5rem' }}>
                  <input
                    type="password" value={newPassword} autoComplete="new-password"
                    onChange={e => setNewPassword(e.target.value)}
                    onKeyDown={e => { if (e.key === 'Enter' && newPassword) storePassword(a.site); }}
                    style={input}
                  />
                  <button style={button('#f97316')} disabled={!newPassword || busy} onClick={() => storePassword(a.site)}>
                    Store
                  </button>
                </div>
              </div>
            )}
          </div>
        ))}
      </div>

      <div style={card}>
        <div style={{ fontSize: '0.95rem', fontWeight: 600, marginBottom: '0.15rem' }}>
          {editing ? `Edit ${editing}` : 'Add an account'}
        </div>
        <p style={{ fontSize: '0.75rem', color: '#666', marginBottom: '0.9rem' }}>
          Domains are the only hosts this session may be used on — and the only ones a password will ever be typed into.
        </p>

        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: '0.75rem' }}>
          <div>
            <label style={label}>Site</label>
            <input style={input} value={form.site} disabled={!!editing} placeholder="airbnb"
              onChange={e => setForm({ ...form, site: e.target.value })} />
          </div>
          <div>
            <label style={label}>Domains (comma separated)</label>
            <input style={input} value={form.domains} placeholder="airbnb.com, airbnb.ru"
              onChange={e => setForm({ ...form, domains: e.target.value })} />
          </div>
          <div>
            <label style={label}>Login</label>
            <input style={input} value={form.login} placeholder="you@example.com" autoComplete="off"
              onChange={e => setForm({ ...form, login: e.target.value })} />
          </div>
          <div>
            <label style={label}>Login page (optional)</label>
            <input style={input} value={form.login_url} placeholder="https://www.airbnb.com/login"
              onChange={e => setForm({ ...form, login_url: e.target.value })} />
          </div>
          <div>
            <label style={label}>Browser profile directory (blank = one per site)</label>
            <input style={input} value={form.profile_dir} placeholder="/data/browser-profiles/airbnb"
              onChange={e => setForm({ ...form, profile_dir: e.target.value })} />
          </div>
          <div>
            <label style={label}>Password (optional — lets it sign in again by itself)</label>
            <input style={input} type="password" value={form.password} autoComplete="new-password"
              disabled={!!vaultOff}
              onChange={e => setForm({ ...form, password: e.target.value })} />
          </div>
          <div>
            <label style={label}>Permission</label>
            <select style={input} value={form.permission} onChange={e => setForm({ ...form, permission: e.target.value })}>
              {(data?.permissions || ['read']).map(p => (
                <option key={p} value={p}>{p} — {PERMISSION_MEANING[p]}</option>
              ))}
            </select>
          </div>
          <div>
            <label style={label}>Notes</label>
            <input style={input} value={form.notes} onChange={e => setForm({ ...form, notes: e.target.value })} />
          </div>
        </div>

        <label style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', margin: '0.9rem 0', fontSize: '0.8rem', color: '#999' }}>
          <input type="checkbox" checked={form.approval_required}
            onChange={e => setForm({ ...form, approval_required: e.target.checked })} />
          Ask me before anything that leaves a trace under my name (messages, bookings, orders)
        </label>

        <div style={{ display: 'flex', gap: '0.5rem' }}>
          <button style={button('#f97316')} disabled={busy || !form.site} onClick={save}>
            {editing ? 'Save changes' : 'Add account'}
          </button>
          {editing && (
            <button style={ghost} onClick={() => { setEditing(null); setForm({ ...EMPTY }); }}>Cancel</button>
          )}
        </div>
      </div>
    </div>
  );
}
