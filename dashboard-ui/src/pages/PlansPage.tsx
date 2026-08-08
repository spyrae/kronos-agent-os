import { useEffect, useState } from 'react';
import { api } from '../api/client';

/**
 * Long-lived plans: work that spends most of its life waiting.
 *
 * A waiting plan is invisible everywhere else — no log line, no turn in flight,
 * nothing on /health. This page exists so "still watching that price, checked 41
 * times" is something a person can see, and so a plan that parked itself waiting
 * for the owner can be released without editing SQLite.
 */

interface Step {
  id: number;
  seq: number;
  title: string;
  prompt: string;
  state: string;
  depends_on: number[];
  waiting_for: string;
  wake_at: number;
  checks: number;
  attempts: number;
  notify: boolean;
  result: string;
  updated_at: number;
}

interface Plan {
  id: number;
  goal: string;
  state: string;
  summary: string;
  created_at: number;
  updated_at: number;
  expires_at: number;
  step_count: number;
  done_count: number;
  failed_count: number;
  waiting_count: number;
  steps?: Step[];
}

const STATE_COLOR: Record<string, string> = {
  active: '#22d3ee',
  done: '#22c55e',
  failed: '#ef4444',
  cancelled: '#64748b',
  pending: '#94a3b8',
  waiting: '#f59e0b',
  running: '#22d3ee',
};

const card: React.CSSProperties = {
  background: '#111', border: '1px solid #1a1a1a', borderRadius: 12, padding: '1.1rem',
};
const ghost: React.CSSProperties = {
  padding: '0.35rem 0.75rem', borderRadius: 8, border: '1px solid #222', cursor: 'pointer',
  background: 'transparent', color: '#999', fontSize: '0.76rem',
};

function when(ts: number): string {
  if (!ts) return '';
  const diff = Math.floor(Date.now() / 1000 - ts);
  if (Math.abs(diff) < 60) return 'just now';
  const ahead = diff < 0;
  const mins = Math.floor(Math.abs(diff) / 60);
  const text = mins < 60 ? `${mins}m` : mins < 1440 ? `${Math.floor(mins / 60)}h` : `${Math.floor(mins / 1440)}d`;
  return ahead ? `in ${text}` : `${text} ago`;
}

export default function PlansPage() {
  const [plans, setPlans] = useState<Plan[]>([]);
  const [showAll, setShowAll] = useState(false);
  const [open, setOpen] = useState<number | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const load = (all = showAll) =>
    api<{ plans: Plan[] }>(`/api/plans?all=${all}`)
      .then(r => setPlans(r.plans))
      .catch(e => setError(String(e)));

  useEffect(() => { load(); }, [showAll]);

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError('');
    try {
      await fn();
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message.replace(/^\d+:\s*/, '') : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1.25rem' }}>
        <div>
          <h1 style={{ fontSize: '1.5rem', fontWeight: 600, marginBottom: '0.2rem' }}>Plans</h1>
          <p style={{ fontSize: '0.82rem', color: '#666' }}>
            Work that outlives a conversation — mostly spent waiting, which is the point.
          </p>
        </div>
        <button style={ghost} onClick={() => setShowAll(!showAll)}>
          {showAll ? 'Running only' : 'Include finished'}
        </button>
      </div>

      {error && (
        <div style={{ ...card, borderColor: '#ef444455', color: '#fca5a5', marginBottom: '1rem', fontSize: '0.85rem' }}>
          {error}
        </div>
      )}

      {plans.length === 0 && (
        <div style={{ ...card, color: '#666', fontSize: '0.85rem' }}>
          {showAll ? 'No plans yet.' : 'Nothing running. Finished plans are behind “Include finished”.'}
        </div>
      )}

      <div style={{ display: 'grid', gap: '0.75rem' }}>
        {plans.map(plan => (
          <div key={plan.id} style={card}>
            <div style={{ display: 'flex', justifyContent: 'space-between', gap: '1rem', alignItems: 'flex-start' }}>
              <div style={{ minWidth: 0 }}>
                <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center', marginBottom: '0.3rem' }}>
                  <span style={{ color: '#555', fontSize: '0.8rem' }}>#{plan.id}</span>
                  <span style={{ fontSize: '0.98rem', fontWeight: 600 }}>{plan.goal}</span>
                  <span style={{ fontSize: '0.68rem', color: STATE_COLOR[plan.state] || '#666' }}>● {plan.state}</span>
                </div>
                <div style={{ fontSize: '0.75rem', color: '#666' }}>
                  {plan.done_count}/{plan.step_count} steps done
                  {plan.failed_count > 0 && ` · ${plan.failed_count} failed`}
                  {plan.waiting_count > 0 && ` · ${plan.waiting_count} waiting`}
                  {' · touched '}{when(plan.updated_at)}
                  {plan.state === 'active' && plan.expires_at ? ` · expires ${when(plan.expires_at)}` : ''}
                </div>
                {plan.summary && (
                  <div style={{ marginTop: '0.6rem', fontSize: '0.82rem', color: '#bbb', whiteSpace: 'pre-wrap' }}>
                    {plan.summary}
                  </div>
                )}
              </div>
              <div style={{ display: 'flex', gap: '0.4rem', flexShrink: 0 }}>
                <button style={ghost} onClick={() => setOpen(open === plan.id ? null : plan.id)}>
                  {open === plan.id ? 'Hide steps' : 'Steps'}
                </button>
                {plan.waiting_count > 0 && (
                  <button
                    style={{ ...ghost, color: '#f59e0b' }}
                    disabled={busy}
                    onClick={() => act(() => api(`/api/plans/${plan.id}/resume`, { method: 'POST' }))}
                  >Resume</button>
                )}
                {plan.state === 'active' && (
                  <button
                    style={{ ...ghost, color: '#ef4444' }}
                    disabled={busy}
                    onClick={() => { if (confirm(`Stop plan #${plan.id}?`)) act(() => api(`/api/plans/${plan.id}`, { method: 'DELETE' })); }}
                  >Stop</button>
                )}
              </div>
            </div>

            {open === plan.id && (
              <div style={{ marginTop: '0.9rem', paddingTop: '0.9rem', borderTop: '1px solid #1a1a1a', display: 'grid', gap: '0.6rem' }}>
                {(plan.steps || []).map(step => (
                  <div key={step.id} style={{ fontSize: '0.8rem' }}>
                    <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'baseline' }}>
                      <span style={{ color: '#555' }}>#{step.id}</span>
                      <span style={{ color: '#ddd', fontWeight: 500 }}>{step.title || `step ${step.seq}`}</span>
                      <span style={{ color: STATE_COLOR[step.state] || '#666', fontSize: '0.7rem' }}>{step.state}</span>
                      {step.notify && <span style={{ color: '#8b5cf6', fontSize: '0.68rem' }}>notifies</span>}
                    </div>
                    <div style={{ color: '#777', marginTop: '0.15rem' }}>{step.prompt}</div>
                    {step.waiting_for && (
                      <div style={{ color: '#f59e0b', marginTop: '0.15rem', fontSize: '0.75rem' }}>
                        waits {step.waiting_for}
                        {step.checks > 0 && ` · checked ${step.checks}×`}
                        {step.wake_at ? ` · next look ${when(step.wake_at)}` : ''}
                      </div>
                    )}
                    {step.depends_on.length > 0 && (
                      <div style={{ color: '#555', marginTop: '0.15rem', fontSize: '0.72rem' }}>
                        after {step.depends_on.map(d => `#${d}`).join(', ')}
                      </div>
                    )}
                    {step.result && (
                      <div style={{ color: '#9ca3af', marginTop: '0.3rem', whiteSpace: 'pre-wrap', fontSize: '0.78rem' }}>
                        → {step.result}
                      </div>
                    )}
                    {step.state === 'waiting' && (
                      <button
                        style={{ ...ghost, marginTop: '0.35rem', fontSize: '0.7rem' }}
                        disabled={busy}
                        onClick={() => act(() => api(`/api/plans/${plan.id}/resume?step=${step.id}`, { method: 'POST' }))}
                      >Release this step</button>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
