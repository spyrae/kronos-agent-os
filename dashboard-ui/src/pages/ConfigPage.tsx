import { useEffect, useState } from 'react';
import type { CSSProperties } from 'react';
import { api } from '../api/client';
import { SectionHeader, StatusBadge } from '../components/Charts';

interface EnvVar { key: string; value: string; is_secret: boolean; }
interface LlmConfig { tiers: Record<string, { model: string; temperature?: number; max_tokens?: number }>; routing: { complex_patterns_count: number; simple_patterns_count: number } }
interface Capability {
  key: string;
  name: string;
  enabled: boolean;
  status: string;
  risk: string;
  description: string;
  required_env: string;
  scope: string;
  owner: string;
  change_mode: string;
  can_request_change: boolean;
}
interface Approval {
  id: string;
  kind: string;
  capability: string;
  capability_name: string;
  action: string;
  status: string;
  risk: string;
  scope: string;
  owner: string;
  required_env: string;
  reason: string;
  requested_at: string;
  decision_reason?: string;
  decided_at?: string;
  effect?: string;
}
interface ApprovalResponse { approvals: Approval[]; pending: number; recent: Approval[] }

const TIER_COLORS: Record<string, string> = {
  standard: '#3b82f6', lite: '#4ade80', fallback: '#f59e0b', vision: '#8b5cf6',
};

const RISK_COLORS: Record<string, string> = {
  protective: '#4ade80',
  high: '#f59e0b',
  critical: '#ef4444',
};

const STATUS_COLORS: Record<string, string> = {
  enabled: '#4ade80',
  blocked: '#71717a',
};

export default function ConfigPage() {
  const [vars, setVars] = useState<EnvVar[]>([]);
  const [llm, setLlm] = useState<LlmConfig | null>(null);
  const [capabilities, setCapabilities] = useState<Capability[]>([]);
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [approvalReason, setApprovalReason] = useState<Record<string, string>>({});
  const [editing, setEditing] = useState<string | null>(null);
  const [editValue, setEditValue] = useState('');
  const [toast, setToast] = useState('');
  const [error, setError] = useState('');

  const showToast = (msg: string) => { setToast(msg); setTimeout(() => setToast(''), 3000); };
  const showError = (msg: string) => { setError(msg); setTimeout(() => setError(''), 5000); };

  const load = async () => {
    try {
      const [envR, llmR, capabilitiesR] = await Promise.all([
        api<{ vars: EnvVar[] }>('/api/config/env'),
        api<LlmConfig>('/api/config/llm'),
        api<{ capabilities: Capability[] }>('/api/config/capabilities'),
      ]);
      const approvalsR = await api<ApprovalResponse>('/api/config/approvals');
      setVars(envR.vars);
      setLlm(llmR);
      setCapabilities(capabilitiesR.capabilities);
      setApprovals(approvalsR.approvals);
    } catch (e: any) { showError(e.message); }
  };
  useEffect(() => { load(); }, []);

  const startEdit = (key: string) => { setEditing(key); setEditValue(''); };

  const saveVar = async () => {
    if (!editing || !editValue) return;
    try {
      await api(`/api/config/env/${editing}`, { method: 'PUT', body: JSON.stringify({ value: editValue }) });
      showToast(`${editing} updated. Restart required.`);
      setEditing(null);
      setEditValue('');
      load();
    } catch (e: any) { showError(e.message); }
  };

  const requestCapabilityChange = async (capability: Capability) => {
    const action = capability.enabled ? 'disable' : 'enable';
    try {
      await api('/api/config/approvals', {
        method: 'POST',
        body: JSON.stringify({ capability: capability.key, action, reason: `dashboard:${action}` }),
      });
      showToast(`${capability.name}: ${action} requested.`);
      load();
    } catch (e: any) { showError(e.message); }
  };

  const decideApproval = async (approval: Approval, decision: 'approved' | 'denied') => {
    try {
      await api(`/api/config/approvals/${approval.id}/decision`, {
        method: 'POST',
        body: JSON.stringify({ decision, reason: approvalReason[approval.id] || '' }),
      });
      showToast(`${approval.capability_name}: ${decision}.`);
      setApprovalReason(prev => ({ ...prev, [approval.id]: '' }));
      load();
    } catch (e: any) { showError(e.message); }
  };

  const card: CSSProperties = {
    background: '#111', border: '1px solid #1a1a1a', borderRadius: 8, padding: '1.25rem',
  };

  const pendingApprovals = approvals.filter(item => item.status === 'pending');
  const recentApprovals = approvals.filter(item => item.status !== 'pending').slice(0, 8);

  return (
    <div>
      {/* Toast / Error */}
      {toast && <div style={{ position: 'fixed', top: '1rem', right: '1rem', padding: '0.75rem 1.5rem', borderRadius: 8, background: '#166534', color: '#fff', fontSize: '0.85rem', zIndex: 100, boxShadow: '0 4px 20px rgba(0,0,0,0.4)', border: '1px solid #4ade8033' }}>{toast}</div>}
      {error && <div style={{ position: 'fixed', top: '1rem', right: '1rem', padding: '0.75rem 1.5rem', borderRadius: 8, background: '#991b1b', color: '#fff', fontSize: '0.85rem', zIndex: 100, boxShadow: '0 4px 20px rgba(0,0,0,0.4)', border: '1px solid #ef444433' }}>{error}</div>}

      <h1 style={{ fontSize: '1.5rem', fontWeight: 600, marginBottom: '1.5rem' }}>Settings</h1>

      {/* LLM Models */}
      {llm && (
        <div style={{ marginBottom: '1.5rem' }}>
          <SectionHeader title="LLM Models" />
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(260px, 1fr))', gap: '1rem' }}>
            {Object.entries(llm.tiers).map(([tier, config]) => (
              <div key={tier} style={{
                ...card,
                borderLeft: `3px solid ${TIER_COLORS[tier] || '#666'}`,
              }}>
                <div style={{
                  fontSize: '0.65rem', fontWeight: 600, color: '#555', textTransform: 'uppercase',
                  letterSpacing: '0.06em', marginBottom: '0.4rem',
                }}>{tier}</div>
                <div style={{ fontSize: '1rem', fontWeight: 600, color: TIER_COLORS[tier] || '#fff', marginBottom: '0.5rem' }}>{config.model}</div>
                <div style={{ display: 'flex', gap: '1rem', fontSize: '0.75rem', color: '#666' }}>
                  {config.temperature !== null && config.temperature !== undefined && (
                    <span>Temp: <span style={{ color: '#999' }}>{config.temperature}</span></span>
                  )}
                  {config.max_tokens !== null && config.max_tokens !== undefined && (
                    <span>Max: <span style={{ color: '#999' }}>{config.max_tokens?.toLocaleString()}</span></span>
                  )}
                </div>
              </div>
            ))}
          </div>
          <div style={{
            marginTop: '0.75rem', fontSize: '0.72rem', color: '#444',
            display: 'flex', gap: '1rem',
          }}>
            <span>Routing: {llm.routing.complex_patterns_count} complex patterns</span>
            <span>{llm.routing.simple_patterns_count} simple patterns</span>
          </div>
        </div>
      )}

      {/* Capability Gates */}
      <div style={{ marginBottom: '1.5rem' }}>
        <SectionHeader title="Capability Gates" />
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))', gap: '1rem' }}>
          {capabilities.map(capability => (
            <div key={capability.key} style={{
              ...card,
              borderLeft: `3px solid ${capability.enabled ? STATUS_COLORS.enabled : RISK_COLORS[capability.risk] || '#666'}`,
            }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', gap: '0.75rem', alignItems: 'flex-start', marginBottom: '0.65rem' }}>
                <div>
                  <div style={{ fontSize: '0.95rem', fontWeight: 600, color: '#e5e5e5', marginBottom: '0.25rem' }}>{capability.name}</div>
                  <div style={{ fontSize: '0.72rem', color: '#555', fontFamily: 'monospace' }}>{capability.key}</div>
                </div>
                <span style={{
                  padding: '0.2rem 0.5rem',
                  borderRadius: 999,
                  background: capability.enabled ? '#16653422' : '#27272a',
                  border: `1px solid ${capability.enabled ? '#4ade8033' : '#3f3f46'}`,
                  color: STATUS_COLORS[capability.status] || '#888',
                  fontSize: '0.68rem',
                  fontWeight: 700,
                  textTransform: 'uppercase',
                }}>{capability.status}</span>
              </div>
              <p style={{ color: '#888', fontSize: '0.78rem', lineHeight: 1.5, margin: '0 0 0.75rem' }}>{capability.description}</p>
              <div style={{ display: 'flex', justifyContent: 'space-between', gap: '0.75rem', alignItems: 'center', fontSize: '0.72rem' }}>
                <span style={{ color: RISK_COLORS[capability.risk] || '#888', fontWeight: 600, textTransform: 'uppercase' }}>{capability.risk}</span>
                <code style={{ color: '#666', fontSize: '0.68rem', overflowWrap: 'anywhere', textAlign: 'right' }}>{capability.required_env}</code>
              </div>
              <div style={{ display: 'flex', justifyContent: 'space-between', gap: '0.75rem', alignItems: 'center', marginTop: '0.85rem' }}>
                <span style={{ color: '#555', fontSize: '0.7rem', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{capability.scope} · {capability.change_mode.replace(/_/g, ' ')}</span>
                <button
                  onClick={() => requestCapabilityChange(capability)}
                  disabled={!capability.can_request_change}
                  style={{
                    padding: '0.35rem 0.65rem', borderRadius: 6, border: '1px solid #2a2a2a',
                    background: '#0a0a0a', color: capability.enabled ? '#f59e0b' : '#f97316',
                    cursor: capability.can_request_change ? 'pointer' : 'not-allowed',
                    fontSize: '0.72rem', fontWeight: 650,
                  }}
                >
                  Request {capability.enabled ? 'disable' : 'enable'}
                </button>
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* Approval Queue */}
      <div style={{ marginBottom: '1.5rem' }}>
        <SectionHeader title="Approval Queue" action={<span style={{ color: '#888', fontSize: '0.75rem' }}>{pendingApprovals.length} pending</span>} />
        <div style={{ ...card, padding: 0, overflow: 'hidden' }}>
          {pendingApprovals.length === 0 && (
            <div style={{ padding: '1.6rem', color: '#555', textAlign: 'center', fontSize: '0.85rem' }}>No pending approvals</div>
          )}
          {pendingApprovals.map(approval => (
            <div key={approval.id} style={{ padding: '1rem', borderBottom: '1px solid #181818', display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) 260px', gap: '1rem', alignItems: 'start' }}>
              <div style={{ minWidth: 0 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.55rem', flexWrap: 'wrap', marginBottom: '0.35rem' }}>
                  <span style={{ color: '#e5e5e5', fontWeight: 650 }}>{approval.capability_name}</span>
                  <StatusBadge status="warning" label={approval.action} />
                  <span style={{ color: RISK_COLORS[approval.risk] || '#888', fontSize: '0.68rem', fontWeight: 700, textTransform: 'uppercase' }}>{approval.risk}</span>
                </div>
                <div style={{ color: '#777', fontSize: '0.75rem', lineHeight: 1.45, overflowWrap: 'anywhere' }}>
                  {approval.required_env}
                </div>
                <div style={{ color: '#555', fontSize: '0.7rem', marginTop: '0.4rem' }}>
                  {approval.id} · {approval.owner}
                </div>
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: '0.45rem' }}>
                <input
                  value={approvalReason[approval.id] || ''}
                  onChange={e => setApprovalReason(prev => ({ ...prev, [approval.id]: e.target.value }))}
                  placeholder="Reason"
                  style={{
                    background: '#0a0a0a', color: '#ddd', border: '1px solid #222',
                    borderRadius: 6, padding: '0.45rem 0.55rem', fontSize: '0.78rem',
                  }}
                />
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0.45rem' }}>
                  <button onClick={() => decideApproval(approval, 'approved')} style={{
                    padding: '0.45rem 0.7rem', borderRadius: 6, border: '1px solid #166534',
                    background: '#052e16', color: '#4ade80', cursor: 'pointer', fontWeight: 650,
                  }}>Approve</button>
                  <button onClick={() => decideApproval(approval, 'denied')} style={{
                    padding: '0.45rem 0.7rem', borderRadius: 6, border: '1px solid #7f1d1d',
                    background: '#1a0505', color: '#fca5a5', cursor: 'pointer', fontWeight: 650,
                  }}>Deny</button>
                </div>
              </div>
            </div>
          ))}
          {recentApprovals.length > 0 && (
            <div style={{ padding: '0.85rem 1rem', borderTop: pendingApprovals.length ? '1px solid #222' : undefined }}>
              <div style={{ color: '#666', fontSize: '0.68rem', textTransform: 'uppercase', letterSpacing: '0.04em', marginBottom: '0.55rem' }}>Recent Decisions</div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: '0.35rem' }}>
                {recentApprovals.map(approval => (
                  <div key={approval.id} style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) auto', gap: '0.75rem', color: '#777', fontSize: '0.75rem' }}>
                    <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{approval.capability_name} · {approval.action}</span>
                    <StatusBadge status={approval.status === 'approved' ? 'running' : 'error'} label={approval.status} />
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Env Vars */}
      <SectionHeader title="Environment Variables" />
      <div style={{ ...card, padding: 0, overflow: 'hidden' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.82rem' }}>
          <thead>
            <tr style={{ borderBottom: '1px solid #1a1a1a' }}>
              {['Key', 'Value', ''].map(h => (
                <th key={h} style={{
                  padding: '0.65rem 1rem', textAlign: 'left', color: '#555',
                  fontWeight: 500, fontSize: '0.72rem', textTransform: 'uppercase', letterSpacing: '0.04em',
                }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {vars.map(v => (
              <tr key={v.key} style={{ borderBottom: '1px solid #111' }}>
                <td style={{ padding: '0.55rem 1rem', fontFamily: 'monospace', color: '#e0e0e0', fontSize: '0.8rem' }}>{v.key}</td>
                <td style={{ padding: '0.55rem 1rem', fontFamily: 'monospace', color: v.is_secret ? '#444' : '#4ade80', fontSize: '0.8rem' }}>
                  {editing === v.key ? (
                    <div style={{ display: 'flex', gap: '0.3rem' }}>
                      <input
                        value={editValue} onChange={e => setEditValue(e.target.value)}
                        onKeyDown={e => { if (e.key === 'Enter') saveVar(); if (e.key === 'Escape') setEditing(null); }}
                        placeholder="New value" autoFocus
                        style={{
                          padding: '0.35rem 0.6rem', borderRadius: 6, border: '1px solid #333',
                          background: '#0a0a0a', color: '#fff', flex: 1, fontFamily: 'monospace', fontSize: '0.8rem', outline: 'none',
                        }}
                      />
                      <button onClick={saveVar} style={{
                        padding: '0.35rem 0.75rem', borderRadius: 6, border: 'none',
                        background: '#f97316', color: '#fff', cursor: 'pointer', fontSize: '0.75rem', fontWeight: 600,
                      }}>Save</button>
                      <button onClick={() => setEditing(null)} style={{
                        padding: '0.35rem 0.5rem', borderRadius: 6, border: '1px solid #222',
                        background: 'transparent', color: '#888', cursor: 'pointer', fontSize: '0.75rem',
                      }}>{'\u2715'}</button>
                    </div>
                  ) : v.value}
                </td>
                <td style={{ padding: '0.55rem 1rem', textAlign: 'right' }}>
                  {editing !== v.key && (
                    <button onClick={() => startEdit(v.key)} style={{
                      padding: '0.2rem 0.6rem', borderRadius: 4, border: '1px solid #222',
                      cursor: 'pointer', fontSize: '0.72rem', background: 'transparent', color: '#888',
                    }}>Edit</button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div style={{ marginTop: '0.5rem', fontSize: '0.72rem', color: '#444' }}>
        Changes require service restart to take effect.
      </div>
    </div>
  );
}
