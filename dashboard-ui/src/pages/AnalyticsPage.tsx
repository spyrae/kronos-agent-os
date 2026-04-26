import { useEffect, useState } from 'react';
import { api } from '../api/client';
import { SectionHeader, LineChart, DonutChart } from '../components/Charts';

interface CostDay { date: string; cost_usd: number; requests: number }
interface RequestEntry { ts: string; tier: string; duration_ms: number; input_preview: string; approx_cost_usd: number }

export default function AnalyticsPage() {
  const [days, setDays] = useState<CostDay[]>([]);
  const [requests, setRequests] = useState<RequestEntry[]>([]);
  const [range, setRange] = useState(7);

  useEffect(() => {
    api<{ days: CostDay[] }>(`/api/monitoring/cost?days=${range}`).then(r => setDays(r.days)).catch(() => {});
    api<{ requests: RequestEntry[] }>('/api/monitoring/requests?limit=100').then(r => setRequests(r.requests)).catch(() => {});
  }, [range]);

  const card: React.CSSProperties = {
    background: '#111', border: '1px solid #1a1a1a', borderRadius: '12px', padding: '1.25rem',
  };

  const totalCost = days.reduce((s, d) => s + d.cost_usd, 0);
  const totalRequests = days.reduce((s, d) => s + d.requests, 0);
  const avgCostPerRequest = totalRequests > 0 ? totalCost / totalRequests : 0;

  // Tier breakdown for donut
  const tierCounts: Record<string, number> = {};
  requests.forEach(r => { tierCounts[r.tier] = (tierCounts[r.tier] || 0) + 1; });
  const tierColors: Record<string, string> = { standard: '#3b82f6', lite: '#4ade80', fallback: '#f59e0b' };

  // Avg latency per day from requests
  const latencyByDay: Record<string, number[]> = {};
  requests.forEach(r => {
    const date = (r.ts || '').slice(0, 10);
    if (!latencyByDay[date]) latencyByDay[date] = [];
    if (r.duration_ms) latencyByDay[date].push(r.duration_ms);
  });

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1.25rem' }}>
        <h1 style={{ fontSize: '1.5rem', fontWeight: 600 }}>Analytics</h1>
        <div style={{ display: 'flex', gap: '0.25rem', background: '#111', borderRadius: 8, padding: '0.2rem', border: '1px solid #1a1a1a' }}>
          {[7, 14, 30].map(d => (
            <button key={d} onClick={() => setRange(d)} style={{
              padding: '0.35rem 0.75rem', borderRadius: 6, border: 'none', cursor: 'pointer',
              background: range === d ? '#f97316' : 'transparent',
              color: range === d ? '#fff' : '#666', fontSize: '0.78rem', fontWeight: range === d ? 600 : 400,
            }}>{d}d</button>
          ))}
        </div>
      </div>

      {/* Summary cards */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: '1rem', marginBottom: '1.5rem' }}>
        <div style={{ ...card, textAlign: 'center' }}>
          <div style={{ fontSize: '0.72rem', color: '#666', marginBottom: '0.3rem', textTransform: 'uppercase' }}>Total Cost</div>
          <div style={{ fontSize: '1.5rem', fontWeight: 700, color: '#f97316' }}>${totalCost.toFixed(4)}</div>
        </div>
        <div style={{ ...card, textAlign: 'center' }}>
          <div style={{ fontSize: '0.72rem', color: '#666', marginBottom: '0.3rem', textTransform: 'uppercase' }}>Total Requests</div>
          <div style={{ fontSize: '1.5rem', fontWeight: 700, color: '#3b82f6' }}>{totalRequests}</div>
        </div>
        <div style={{ ...card, textAlign: 'center' }}>
          <div style={{ fontSize: '0.72rem', color: '#666', marginBottom: '0.3rem', textTransform: 'uppercase' }}>Avg Cost / Request</div>
          <div style={{ fontSize: '1.5rem', fontWeight: 700, color: '#4ade80' }}>${avgCostPerRequest.toFixed(5)}</div>
        </div>
      </div>

      {/* Cost trend chart */}
      <div style={{ ...card, marginBottom: '1rem' }}>
        <SectionHeader title="Cost Trend" />
        <LineChart
          data={days.map(d => ({ label: d.date.slice(5), value: d.cost_usd }))}
          height={180}
          color="#f97316"
        />
      </div>

      {/* Requests trend + Tier breakdown */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 300px', gap: '1rem', marginBottom: '1rem' }}>
        <div style={card}>
          <SectionHeader title="Requests Trend" />
          <LineChart
            data={days.map(d => ({ label: d.date.slice(5), value: d.requests }))}
            height={160}
            color="#3b82f6"
          />
        </div>

        <div style={card}>
          <SectionHeader title="Tier Breakdown" />
          <div style={{ display: 'flex', justifyContent: 'center', marginBottom: '1rem' }}>
            <DonutChart
              value={Object.values(tierCounts).reduce((s, v) => s + v, 0)}
              max={Object.values(tierCounts).reduce((s, v) => s + v, 0) || 1}
              size={120}
              color="#3b82f6"
              label="Requests"
            />
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '0.4rem' }}>
            {Object.entries(tierCounts).map(([tier, count]) => (
              <div key={tier} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '0.35rem 0.5rem', background: '#0a0a0a', borderRadius: 6 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.4rem' }}>
                  <span style={{ width: 8, height: 8, borderRadius: 2, background: tierColors[tier] || '#666' }} />
                  <span style={{ fontSize: '0.8rem', color: '#ccc', textTransform: 'capitalize' }}>{tier}</span>
                </div>
                <span style={{ fontSize: '0.8rem', fontWeight: 600, color: '#fff' }}>{count}</span>
              </div>
            ))}
            {Object.keys(tierCounts).length === 0 && (
              <div style={{ textAlign: 'center', color: '#555', padding: '1rem', fontSize: '0.85rem' }}>No data</div>
            )}
          </div>
        </div>
      </div>

      {/* Daily breakdown table */}
      <div style={card}>
        <SectionHeader title="Daily Breakdown" />
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.82rem' }}>
          <thead>
            <tr style={{ borderBottom: '1px solid #1a1a1a' }}>
              {['Date', 'Requests', 'Cost (USD)', 'Avg Cost/Req'].map(h => (
                <th key={h} style={{ padding: '0.5rem 0.6rem', textAlign: 'left', color: '#555', fontWeight: 500, fontSize: '0.72rem', textTransform: 'uppercase' }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {[...days].reverse().map(d => (
              <tr key={d.date} style={{ borderBottom: '1px solid #111' }}>
                <td style={{ padding: '0.5rem 0.6rem', color: '#ccc' }}>{d.date}</td>
                <td style={{ padding: '0.5rem 0.6rem', color: '#3b82f6', fontWeight: 500 }}>{d.requests}</td>
                <td style={{ padding: '0.5rem 0.6rem', color: '#f97316', fontWeight: 500 }}>${d.cost_usd.toFixed(4)}</td>
                <td style={{ padding: '0.5rem 0.6rem', color: '#888' }}>
                  ${d.requests > 0 ? (d.cost_usd / d.requests).toFixed(5) : '0'}
                </td>
              </tr>
            ))}
            {days.length === 0 && (
              <tr><td colSpan={4} style={{ padding: '2rem', textAlign: 'center', color: '#555' }}>No cost data recorded</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
