import { useEffect, useState } from 'react';
import { api } from '../api/client';

interface SandboxStatus {
  basic_sandbox: {
    docker_available: boolean;
    image: string;
    image_available: boolean;
    build_script: string;
  };
  platform: {
    ready: boolean;
    execution_ready: boolean;
    network_default: string;
    secret_proxy: string;
    package_policy: string;
    resource_accounting: boolean;
    audit_log: string;
    workspace_root: string;
  };
}

interface SandboxRun {
  run_id: string;
  ts: string;
  status: string;
  tool_name: string;
  session_id: string;
  decision?: { violations?: string[]; reason?: string };
  request?: {
    network_domains?: string[];
    packages?: string[];
    secret_capabilities?: string[];
    resources?: Record<string, unknown>;
  };
  stderr_summary?: string;
}

interface SandboxRunResponse {
  runs: SandboxRun[];
  total: number;
  blocked: number;
}

const cardStyle: React.CSSProperties = {
  background: '#111',
  border: '1px solid #1f1f23',
  borderRadius: 12,
  padding: '1rem',
};

function Badge({ label, tone }: { label: string; tone: 'ok' | 'warn' | 'blocked' }) {
  const colors = {
    ok: ['#052e16', '#4ade80', '#86efac'],
    warn: ['#451a03', '#f97316', '#fdba74'],
    blocked: ['#450a0a', '#ef4444', '#fca5a5'],
  }[tone];
  return (
    <span style={{
      background: colors[0],
      border: `1px solid ${colors[1]}55`,
      color: colors[2],
      borderRadius: 999,
      padding: '0.2rem 0.55rem',
      fontSize: '0.72rem',
      fontWeight: 650,
    }}>{label}</span>
  );
}

function StatusCard({ title, value, detail, tone }: { title: string; value: string; detail: string; tone: 'ok' | 'warn' | 'blocked' }) {
  return (
    <div style={cardStyle}>
      <div style={{ display: 'flex', justifyContent: 'space-between', gap: '1rem', alignItems: 'center' }}>
        <div style={{ color: '#888', fontSize: '0.78rem', textTransform: 'uppercase', letterSpacing: '0.06em' }}>{title}</div>
        <Badge label={value} tone={tone} />
      </div>
      <div style={{ color: '#e5e5e5', fontSize: '0.85rem', lineHeight: 1.5, marginTop: '0.8rem' }}>{detail}</div>
    </div>
  );
}

export default function SandboxPage() {
  const [status, setStatus] = useState<SandboxStatus | null>(null);
  const [runs, setRuns] = useState<SandboxRun[]>([]);
  const [summary, setSummary] = useState<SandboxRunResponse | null>(null);

  useEffect(() => {
    api<SandboxStatus>('/api/sandbox/status').then(setStatus).catch(() => {});
    api<SandboxRunResponse>('/api/sandbox/runs?limit=50').then(response => {
      setRuns(response.runs);
      setSummary(response);
    }).catch(() => {});
  }, []);

  return (
    <div>
      <div style={{ marginBottom: '1.5rem' }}>
        <h1 style={{ color: '#fff', fontSize: '1.6rem', margin: 0 }}>Sandbox Platform</h1>
        <p style={{ color: '#777', margin: '0.4rem 0 0' }}>
          Secret proxy, default-deny network policy, per-session isolation, and audit visibility.
        </p>
      </div>

      <div style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))',
        gap: '1rem',
        marginBottom: '1.5rem',
      }}>
        <StatusCard
          title="Policy engine"
          value={status?.platform.ready ? 'ready' : 'unknown'}
          tone={status?.platform.ready ? 'ok' : 'warn'}
          detail={`Network is ${status?.platform.network_default || 'deny'} by default; packages use ${status?.platform.package_policy || 'allowlist'}.`}
        />
        <StatusCard
          title="Docker execution"
          value={status?.platform.execution_ready ? 'ready' : 'not ready'}
          tone={status?.platform.execution_ready ? 'ok' : 'warn'}
          detail={status?.basic_sandbox.image_available
            ? `Image ${status.basic_sandbox.image} is available.`
            : `Build image with ${status?.basic_sandbox.build_script || 'scripts/build-sandbox.sh'}.`}
        />
        <StatusCard
          title="Secret proxy"
          value={status?.platform.secret_proxy || 'capability'}
          tone="ok"
          detail="Runs declare secret capabilities; raw secrets are redacted from audit records."
        />
        <StatusCard
          title="Recent blocks"
          value={`${summary?.blocked || 0}`}
          tone={(summary?.blocked || 0) > 0 ? 'blocked' : 'ok'}
          detail={`${summary?.total || 0} sandbox audit record(s) loaded from the durable log.`}
        />
      </div>

      <div style={cardStyle}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '0.9rem' }}>
          <h2 style={{ color: '#fff', margin: 0, fontSize: '1rem' }}>Recent executions and policy events</h2>
          <code style={{ color: '#555', fontSize: '0.7rem' }}>{status?.platform.audit_log || 'sandbox_runs.jsonl'}</code>
        </div>
        <div style={{ overflowX: 'auto' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.78rem' }}>
            <thead>
              <tr style={{ borderBottom: '1px solid #222', color: '#666', textAlign: 'left' }}>
                <th style={{ padding: '0.65rem' }}>Time</th>
                <th style={{ padding: '0.65rem' }}>Tool</th>
                <th style={{ padding: '0.65rem' }}>Status</th>
                <th style={{ padding: '0.65rem' }}>Session</th>
                <th style={{ padding: '0.65rem' }}>Policy</th>
                <th style={{ padding: '0.65rem' }}>Request</th>
              </tr>
            </thead>
            <tbody>
              {runs.map(run => {
                const violations = run.decision?.violations || [];
                const requested = [
                  ...(run.request?.network_domains || []).map(item => `net:${item}`),
                  ...(run.request?.packages || []).map(item => `pkg:${item}`),
                  ...(run.request?.secret_capabilities || []).map(item => `secret:${item}`),
                ];
                return (
                  <tr key={run.run_id} style={{ borderBottom: '1px solid #171717' }}>
                    <td style={{ padding: '0.65rem', color: '#777', whiteSpace: 'nowrap' }}>{run.ts}</td>
                    <td style={{ padding: '0.65rem', color: '#e5e5e5', fontWeight: 650 }}>{run.tool_name}</td>
                    <td style={{ padding: '0.65rem' }}>
                      <Badge label={run.status} tone={run.status === 'blocked' ? 'blocked' : 'ok'} />
                    </td>
                    <td style={{ padding: '0.65rem', color: '#888' }}>{run.session_id}</td>
                    <td style={{ padding: '0.65rem', color: violations.length ? '#fca5a5' : '#86efac' }}>
                      {violations.length ? violations.join(', ') : run.decision?.reason || 'allowed'}
                    </td>
                    <td style={{ padding: '0.65rem', color: '#777' }}>{requested.join(', ') || 'local only'}</td>
                  </tr>
                );
              })}
              {runs.length === 0 && (
                <tr>
                  <td colSpan={6} style={{ padding: '2rem', textAlign: 'center', color: '#555' }}>
                    No sandbox audit records yet.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
        <div style={{ marginTop: '0.75rem', color: '#555', fontSize: '0.72rem' }}>
          Workspace root: {status?.platform.workspace_root || 'not loaded'}
        </div>
      </div>
    </div>
  );
}
