import { type CSSProperties, useEffect, useState } from 'react';
import { api } from '../api/client';
import { KPICard, SectionHeader, StatusBadge } from '../components/Charts';

interface Turn {
  turn_id: string;
  thread_id: string;
  status: string;
  input_message: string;
  attempts: number;
  started_at: string;
  completed_at: string | null;
  error: string | null;
}

interface JournalEntry {
  seq: number;
  status: string;
  created_at: string;
  message: {
    type?: string;
    content?: string;
    tool_calls?: { name?: string; args?: Record<string, unknown> }[];
  };
}

interface TurnDetail extends Turn {
  journal: JournalEntry[];
  tool_results: { tool_call_id: string; content: string }[];
  effects: { idempotency_key: string; tool: string; result: string; created_at: string }[];
}

interface TurnsResponse {
  turns: Turn[];
  counts: Record<string, number>;
  total: number;
  running_turns: number;
  oldest_running_age_seconds: number | null;
}

const IN_FLIGHT = ['running', 'resuming'];
const FILTERS = ['all', 'running', 'resuming', 'done', 'failed', 'superseded'] as const;

function badge(status: string): 'running' | 'stopped' | 'error' | 'warning' {
  if (IN_FLIGHT.includes(status)) return 'warning';
  if (status === 'failed') return 'error';
  if (status === 'done') return 'running';
  return 'stopped';
}

function age(seconds: number | null): string {
  if (seconds === null) return '—';
  if (seconds < 90) return `${Math.round(seconds)}s`;
  if (seconds < 5400) return `${Math.round(seconds / 60)}m`;
  return `${Math.round(seconds / 3600)}h`;
}

export default function TurnsPage() {
  const [data, setData] = useState<TurnsResponse | null>(null);
  const [status, setStatus] = useState<string>('all');
  const [selected, setSelected] = useState<TurnDetail | null>(null);
  const [busy, setBusy] = useState('');
  const [notice, setNotice] = useState('');

  const card: CSSProperties = {
    background: '#111',
    border: '1px solid #1a1a1a',
    borderRadius: 8,
    padding: '1.25rem',
  };
  const button: CSSProperties = {
    background: '#181818',
    border: '1px solid #262626',
    borderRadius: 5,
    color: '#ccc',
    cursor: 'pointer',
    fontSize: '0.7rem',
    marginRight: '0.35rem',
    padding: '0.25rem 0.6rem',
  };
  const cell: CSSProperties = { padding: '0.55rem 0.6rem', verticalAlign: 'top' };
  const mono: CSSProperties = { fontFamily: 'ui-monospace, SFMono-Regular, monospace', color: '#9ca3af' };

  const load = () => {
    const query = status === 'all' ? '' : `?status=${status}`;
    api<TurnsResponse>(`/api/turns${query}`)
      .then(setData)
      .catch(() => setNotice('Could not load turns'));
  };

  useEffect(load, [status]);

  const open = (turnId: string) => {
    api<TurnDetail>(`/api/turns/${turnId}`)
      .then(setSelected)
      .catch(() => setNotice(`Could not load turn ${turnId}`));
  };

  const act = async (turnId: string, action: 'resume' | 'fork') => {
    setBusy(turnId);
    setNotice('');
    try {
      const result = await api<{ ok: boolean; answer?: string; thread_id?: string }>(
        `/api/turns/${turnId}/${action}`,
        { method: 'POST', body: JSON.stringify({}) }
      );
      setNotice(
        action === 'resume'
          ? `Turn finished: ${(result.answer ?? '').slice(0, 140)}`
          : `Forked into thread ${result.thread_id}`
      );
      load();
      if (selected?.turn_id === turnId) open(turnId);
    } catch {
      setNotice(`${action} failed — see the agent log`);
    } finally {
      setBusy('');
    }
  };

  return (
    <div>
      <h1 style={{ fontSize: '1.5rem', fontWeight: 600, marginBottom: '0.35rem' }}>Durable Turns</h1>
      <p style={{ color: '#666', fontSize: '0.8rem', marginBottom: '1.25rem', maxWidth: 640 }}>
        A turn stuck in flight means a process died while someone was waiting. Resume finishes it —
        effects already recorded are never repeated.
      </p>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))', gap: '0.75rem', marginBottom: '1.25rem' }}>
        <KPICard icon="D" value={String(data?.running_turns ?? 0)} label="In flight" />
        <KPICard icon="T" value={age(data?.oldest_running_age_seconds ?? null)} label="Oldest in flight" />
        <KPICard icon="L" value={String(data?.total ?? 0)} label="Listed" />
      </div>

      {notice && (
        <div style={{ ...card, marginBottom: '1rem', borderColor: '#3f2d0a', color: '#fcd34d', fontSize: '0.78rem' }}>
          {notice}
        </div>
      )}

      <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.4rem', marginBottom: '0.9rem' }}>
        {FILTERS.map(option => (
          <button
            key={option}
            onClick={() => setStatus(option)}
            style={{
              ...button,
              background: status === option ? '#17120c' : '#181818',
              borderColor: status === option ? '#92400e' : '#262626',
              color: status === option ? '#fcd34d' : '#999',
            }}
          >
            {option}
            {data?.counts?.[option] ? ` (${data.counts[option]})` : ''}
          </button>
        ))}
      </div>

      <div style={{ ...card, marginBottom: '1rem', overflowX: 'auto' }}>
        <SectionHeader title="Turns" />
        <table style={{ width: '100%', minWidth: 760, borderCollapse: 'collapse', fontSize: '0.78rem' }}>
          <thead>
            <tr style={{ borderBottom: '1px solid #222' }}>
              {['Status', 'Thread', 'Input', 'Att', 'Started', ''].map(header => (
                <th
                  key={header}
                  style={{
                    padding: '0.55rem 0.6rem',
                    textAlign: 'left',
                    color: '#555',
                    fontSize: '0.68rem',
                    fontWeight: 600,
                    textTransform: 'uppercase',
                    letterSpacing: '0.04em',
                  }}
                >
                  {header}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {(data?.turns ?? []).map(turn => (
              <tr
                key={turn.turn_id}
                style={{
                  borderBottom: '1px solid #171717',
                  background: selected?.turn_id === turn.turn_id ? '#17120c' : 'transparent',
                }}
              >
                <td style={cell}>
                  <StatusBadge status={badge(turn.status)} label={turn.status} />
                </td>
                <td style={{ ...cell, ...mono }}>{turn.thread_id}</td>
                <td style={{ ...cell, color: '#ccc' }}>{turn.input_message?.slice(0, 60) || '—'}</td>
                <td style={{ ...cell, color: turn.attempts ? '#fcd34d' : '#555' }}>{turn.attempts}</td>
                <td style={{ ...cell, color: '#666', whiteSpace: 'nowrap' }}>{turn.started_at}</td>
                <td style={{ ...cell, whiteSpace: 'nowrap' }}>
                  <button style={button} onClick={() => open(turn.turn_id)}>
                    Details
                  </button>
                  {IN_FLIGHT.includes(turn.status) && (
                    <button
                      style={{ ...button, borderColor: '#92400e', color: '#fcd34d' }}
                      disabled={busy === turn.turn_id}
                      onClick={() => act(turn.turn_id, 'resume')}
                    >
                      {busy === turn.turn_id ? '…' : 'Resume'}
                    </button>
                  )}
                  <button style={button} disabled={busy === turn.turn_id} onClick={() => act(turn.turn_id, 'fork')}>
                    Fork
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {!data?.turns?.length && (
          <p style={{ color: '#666', fontSize: '0.78rem', marginTop: '0.75rem' }}>
            No durable turns recorded for this agent.
          </p>
        )}
      </div>

      {selected && (
        <div style={card}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '0.75rem' }}>
            <h2 style={{ fontSize: '0.95rem', fontWeight: 600, display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
              <span style={mono}>{selected.turn_id.slice(0, 8)}</span>
              <StatusBadge status={badge(selected.status)} label={selected.status} />
            </h2>
            <button style={button} onClick={() => setSelected(null)}>
              Close
            </button>
          </div>

          {selected.error && (
            <p style={{ color: '#fca5a5', fontSize: '0.78rem', marginBottom: '0.6rem' }}>{selected.error}</p>
          )}
          <p style={{ color: '#666', fontSize: '0.72rem', marginBottom: '0.5rem' }}>
            thread <span style={mono}>{selected.thread_id}</span> · attempts {selected.attempts} · started{' '}
            {selected.started_at}
          </p>
          <p style={{ color: '#ccc', fontSize: '0.82rem', marginBottom: '1rem' }}>{selected.input_message}</p>

          <SectionHeader title="Journal" />
          {selected.journal.length ? (
            <ol style={{ margin: '0 0 1rem 1.1rem', color: '#ccc', fontSize: '0.78rem', lineHeight: 1.7 }}>
              {selected.journal.map(entry => {
                const calls = (entry.message?.tool_calls ?? []).map(call => call.name).join(', ');
                return (
                  <li key={entry.seq}>
                    <span style={mono}>{entry.message?.type ?? '?'}</span>{' '}
                    {calls || (entry.message?.content ?? '').slice(0, 140) || '—'}
                  </li>
                );
              })}
            </ol>
          ) : (
            <p style={{ color: '#666', fontSize: '0.76rem', marginBottom: '1rem' }}>
              No journal rows — a finished turn drops them; they exist only while a turn is in flight.
            </p>
          )}

          {selected.effects.length > 0 && (
            <>
              <SectionHeader title="Recorded external effects" />
              <p style={{ color: '#666', fontSize: '0.72rem', marginBottom: '0.4rem' }}>
                These already happened. Resuming this turn will not repeat them.
              </p>
              <ul style={{ margin: '0 0 0 1.1rem', color: '#ccc', fontSize: '0.78rem', lineHeight: 1.7 }}>
                {selected.effects.map(effect => (
                  <li key={effect.idempotency_key}>
                    <span style={mono}>{effect.tool}</span>: {effect.result.slice(0, 100)}
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      )}
    </div>
  );
}
