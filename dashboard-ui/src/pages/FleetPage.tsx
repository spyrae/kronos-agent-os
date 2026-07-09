import { useEffect, useState } from 'react';
import type { CSSProperties } from 'react';
import { api } from '../api/client';
import { SectionHeader } from '../components/Charts';

interface AgentHealth { reachable: boolean; status?: string; telegram_connected?: boolean; }
interface FleetAgent {
  name: string; username: string; role: string; is_me: boolean;
  last_seen: number | null; messages_24h: number; replies_won_24h: number;
  claims_yielded_24h: number; handoffs_pending: number;
  sparkline: number[]; health: AgentHealth | null;
}
interface TimelineEvent { kind: string; ts: number; from_agent: string; to_agent: string; text: string; state: string; }
interface FleetTotals {
  user_messages_24h: number; agent_messages_24h: number; active_councils: number;
  pending_handoffs: number; pending_memory_requests: number; shared_facts: number;
  metrics: Record<string, number>;
}
interface FleetOverview {
  agents: FleetAgent[]; totals: FleetTotals; timeline: TimelineEvent[];
  generated_at: number; health_probes_configured: boolean;
}

// Stable per-agent accent colors — order-independent, keyed by name.
const AGENT_COLORS: Record<string, string> = {
  kronos: '#f97316', nexus: '#3b82f6', lacuna: '#a78bfa',
  resonant: '#4ade80', keystone: '#f59e0b', impulse: '#f43f5e',
};
const FALLBACK_COLORS = ['#06b6d4', '#ec4899', '#14b8a6', '#8b5cf6'];
const agentColor = (name: string) =>
  AGENT_COLORS[name] || FALLBACK_COLORS[name.length % FALLBACK_COLORS.length];

const KIND_STYLE: Record<string, { label: string; color: string; icon: string }> = {
  reply: { label: 'reply', color: '#4ade80', icon: '\u{1F4AC}' },
  handoff: { label: 'hand-off', color: '#06b6d4', icon: '↪️' },
  council: { label: 'council', color: '#a78bfa', icon: '⚖️' },
  memory: { label: 'memory', color: '#f59e0b', icon: '\u{1F9E0}' },
};

function timeAgo(epoch: number | null): string {
  if (!epoch) return 'never';
  const s = Math.max(0, Date.now() / 1000 - epoch);
  if (s < 90) return 'just now';
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

type Liveness = 'online' | 'idle' | 'quiet' | 'down';

function liveness(agent: FleetAgent): Liveness {
  if (agent.health) {
    if (!agent.health.reachable) return 'down';
    return agent.health.telegram_connected ? 'online' : 'idle';
  }
  const seen = agent.last_seen;
  if (!seen) return 'quiet';
  const age = Date.now() / 1000 - seen;
  if (age < 15 * 60) return 'online';
  if (age < 2 * 3600) return 'idle';
  return 'quiet';
}

const LIVENESS_COLOR: Record<Liveness, string> = {
  online: '#4ade80', idle: '#f59e0b', quiet: '#555', down: '#ef4444',
};
const LIVENESS_LABEL: Record<Liveness, string> = {
  online: 'online', idle: 'idle', quiet: 'quiet', down: 'unreachable',
};

function Sparkline({ data, color }: { data: number[]; color: string }) {
  const w = 132, h = 30;
  const max = Math.max(...data, 1);
  const step = w / (data.length - 1 || 1);
  const points = data.map((v, i) => `${(i * step).toFixed(1)},${(h - 3 - (v / max) * (h - 8)).toFixed(1)}`).join(' ');
  const area = `0,${h} ${points} ${w},${h}`;
  const gid = `spark-${color.replace('#', '')}`;
  return (
    <svg width={w} height={h} style={{ display: 'block' }}>
      <defs>
        <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity="0.35" />
          <stop offset="100%" stopColor={color} stopOpacity="0" />
        </linearGradient>
      </defs>
      <polygon points={area} fill={`url(#${gid})`} />
      <polyline points={points} fill="none" stroke={color} strokeWidth="1.6" strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  );
}

function PulseStat({ label, value, accent }: { label: string; value: number | string; accent?: string }) {
  return (
    <div style={{
      background: '#111', border: '1px solid #1a1a1a', borderRadius: 12,
      padding: '0.85rem 1.1rem', minWidth: 0,
    }}>
      <div style={{ color: accent || '#fff', fontSize: '1.45rem', fontWeight: 750, lineHeight: 1.1 }}>{value}</div>
      <div style={{ color: '#666', fontSize: '0.66rem', textTransform: 'uppercase', letterSpacing: '0.06em', marginTop: '0.3rem' }}>{label}</div>
    </div>
  );
}

export default function FleetPage() {
  const [fleet, setFleet] = useState<FleetOverview | null>(null);

  useEffect(() => {
    const load = () => api<FleetOverview>('/api/fleet/overview').then(setFleet).catch(() => {});
    load();
    const interval = setInterval(load, 20000);
    return () => clearInterval(interval);
  }, []);

  const card: CSSProperties = {
    background: '#111', border: '1px solid #1a1a1a', borderRadius: 12, padding: '1.1rem',
  };

  if (!fleet) {
    return <div style={{ color: '#555', padding: '3rem', textAlign: 'center' }}>Loading fleet…</div>;
  }

  const onlineCount = fleet.agents.filter(a => liveness(a) === 'online').length;

  return (
    <div>
      {/* Pulse-dot keyframes for live agents */}
      <style>{`@keyframes fleetPulse { 0% { box-shadow: 0 0 0 0 rgba(74,222,128,0.5); } 70% { box-shadow: 0 0 0 7px rgba(74,222,128,0); } 100% { box-shadow: 0 0 0 0 rgba(74,222,128,0); } }`}</style>

      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: '1.1rem' }}>
        <div>
          <h1 style={{ fontSize: '1.5rem', fontWeight: 600 }}>Fleet</h1>
          <p style={{ fontSize: '0.78rem', color: '#555', marginTop: '0.2rem' }}>
            {fleet.agents.length} agents · {onlineCount} online · one shared ledger
          </p>
        </div>
        {!fleet.health_probes_configured && (
          <span style={{ color: '#444', fontSize: '0.68rem' }}>
            liveness from ledger · set FLEET_HEALTH_PORTS for live probes
          </span>
        )}
      </div>

      {/* Swarm pulse */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))', gap: '0.75rem', marginBottom: '1.25rem' }}>
        <PulseStat label="You said (24h)" value={fleet.totals.user_messages_24h} />
        <PulseStat label="Swarm replied (24h)" value={fleet.totals.agent_messages_24h} accent="#4ade80" />
        <PulseStat label="Active councils" value={fleet.totals.active_councils} accent="#a78bfa" />
        <PulseStat label="Pending hand-offs" value={fleet.totals.pending_handoffs} accent="#06b6d4" />
        <PulseStat label="Shared facts" value={fleet.totals.shared_facts} accent="#f59e0b" />
        <PulseStat label="Dupes avoided" value={fleet.totals.metrics.duplicate_replies_avoided ?? 0} />
      </div>

      {/* Agent cards */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(330px, 1fr))', gap: '1rem', marginBottom: '1.25rem' }}>
        {fleet.agents.map(agent => {
          const color = agentColor(agent.name);
          const live = liveness(agent);
          return (
            <div key={agent.name} style={{
              ...card,
              border: agent.is_me ? `1px solid ${color}55` : '1px solid #1a1a1a',
              background: agent.is_me ? `linear-gradient(160deg, ${color}0e, #111 45%)` : '#111',
              position: 'relative', overflow: 'hidden',
            }}>
              <div style={{ display: 'flex', gap: '0.85rem', alignItems: 'flex-start' }}>
                <div style={{
                  width: 46, height: 46, borderRadius: 13, flexShrink: 0,
                  background: `${color}1c`, border: `1px solid ${color}44`,
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                  color, fontWeight: 800, fontSize: '1.25rem', fontFamily: 'Georgia, serif',
                }}>{agent.name[0].toUpperCase()}</div>
                <div style={{ minWidth: 0, flex: 1 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                    <span style={{ color: '#fff', fontWeight: 700, fontSize: '0.98rem' }}>{agent.name}</span>
                    {agent.is_me && (
                      <span style={{ color, fontSize: '0.6rem', fontWeight: 700, border: `1px solid ${color}55`, borderRadius: 5, padding: '0.08rem 0.4rem', textTransform: 'uppercase', letterSpacing: '0.05em' }}>this dashboard</span>
                    )}
                    <span style={{
                      width: 8, height: 8, borderRadius: '50%', marginLeft: 'auto', flexShrink: 0,
                      background: LIVENESS_COLOR[live],
                      animation: live === 'online' ? 'fleetPulse 2.2s infinite' : undefined,
                    }} title={LIVENESS_LABEL[live]} />
                  </div>
                  <div style={{ color: '#555', fontSize: '0.7rem', fontFamily: 'monospace' }}>@{agent.username}</div>
                  <div style={{ color: '#888', fontSize: '0.74rem', marginTop: '0.35rem', lineHeight: 1.45, minHeight: '2.1em' }}>{agent.role}</div>
                </div>
              </div>

              <div style={{ display: 'flex', alignItems: 'flex-end', justifyContent: 'space-between', marginTop: '0.8rem' }}>
                <div style={{ display: 'flex', gap: '1.1rem' }}>
                  <div>
                    <div style={{ color: '#fff', fontWeight: 750, fontSize: '1.05rem' }}>{agent.replies_won_24h}</div>
                    <div style={{ color: '#555', fontSize: '0.62rem', textTransform: 'uppercase', letterSpacing: '0.05em' }}>replies 24h</div>
                  </div>
                  <div>
                    <div style={{ color: '#fff', fontWeight: 750, fontSize: '1.05rem' }}>{agent.messages_24h}</div>
                    <div style={{ color: '#555', fontSize: '0.62rem', textTransform: 'uppercase', letterSpacing: '0.05em' }}>msgs 24h</div>
                  </div>
                  {agent.handoffs_pending > 0 && (
                    <div>
                      <div style={{ color: '#06b6d4', fontWeight: 750, fontSize: '1.05rem' }}>{agent.handoffs_pending}</div>
                      <div style={{ color: '#555', fontSize: '0.62rem', textTransform: 'uppercase', letterSpacing: '0.05em' }}>hand-offs in</div>
                    </div>
                  )}
                </div>
                <Sparkline data={agent.sparkline} color={color} />
              </div>

              <div style={{ color: '#444', fontSize: '0.66rem', marginTop: '0.55rem' }}>
                last seen {timeAgo(agent.last_seen)}
                {agent.health && !agent.health.reachable && <span style={{ color: '#ef4444' }}> · health unreachable</span>}
              </div>
            </div>
          );
        })}
      </div>

      {/* Coordination feed */}
      <div style={card}>
        <SectionHeader title="Coordination Feed" action={
          <span style={{ color: '#666', fontSize: '0.72rem' }}>replies won · hand-offs · councils · memory asks</span>
        } />
        {fleet.timeline.length === 0 ? (
          <div style={{ color: '#555', fontSize: '0.8rem', padding: '1.5rem', textAlign: 'center' }}>
            No coordination events yet — they appear as the swarm talks, hands off, and convenes councils
          </div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column' }}>
            {fleet.timeline.map((event, index) => {
              const kind = KIND_STYLE[event.kind] || KIND_STYLE.reply;
              const from = agentColor(event.from_agent);
              return (
                <div key={`${event.kind}-${event.ts}-${index}`} style={{
                  display: 'grid', gridTemplateColumns: '20px 92px 1fr auto', gap: '0.7rem',
                  alignItems: 'baseline', padding: '0.5rem 0.25rem',
                  borderBottom: index < fleet.timeline.length - 1 ? '1px solid #161616' : 'none',
                }}>
                  <span style={{ fontSize: '0.8rem' }}>{kind.icon}</span>
                  <span style={{
                    color: kind.color, fontSize: '0.66rem', fontWeight: 700,
                    textTransform: 'uppercase', letterSpacing: '0.05em',
                  }}>{kind.label}</span>
                  <span style={{ minWidth: 0, color: '#bbb', fontSize: '0.78rem', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    <span style={{ color: from, fontWeight: 650 }}>{event.from_agent}</span>
                    {event.to_agent && <span style={{ color: '#555' }}> → {event.to_agent}</span>}
                    <span style={{ color: '#777' }}> · {event.text}</span>
                  </span>
                  <span style={{ color: '#555', fontSize: '0.68rem', whiteSpace: 'nowrap' }}>{timeAgo(event.ts)}</span>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
