import { useEffect, useState, useCallback } from 'react';
import { api } from '../api/client';
import { SectionHeader } from '../components/Charts';

interface GraphNode { id: string; label: string; type: string; }
interface GraphEdge { source: string; target: string; conditional: boolean; }

const NODE_COLORS: Record<string, { bg: string; border: string; text: string }> = {
  special: { bg: '#1a1a2e', border: '#4b5563', text: '#9ca3af' },
  supervisor: { bg: '#1e3a5f', border: '#3b82f6', text: '#fff' },
  agent: { bg: '#1a2e1a', border: '#4ade80', text: '#fff' },
  tool: { bg: '#2e1a1a', border: '#f97316', text: '#fff' },
  default: { bg: '#1e1e3a', border: '#6366f1', text: '#fff' },
};

function getNodeStyle(type: string, label: string) {
  if (type === 'special') return NODE_COLORS.special;
  if (label.includes('supervisor') || label.includes('router')) return NODE_COLORS.supervisor;
  if (label.includes('agent') || label.includes('research') || label.includes('task') || label.includes('finance')) return NODE_COLORS.agent;
  if (label.includes('tool') || label.includes('mcp')) return NODE_COLORS.tool;
  return NODE_COLORS.default;
}

export default function GraphPage() {
  const [nodes, setNodes] = useState<GraphNode[]>([]);
  const [edges, setEdges] = useState<GraphEdge[]>([]);
  const [mermaid, setMermaid] = useState('');
  const [view, setView] = useState<'visual' | 'mermaid'>('visual');
  const [hoveredNode, setHoveredNode] = useState<string | null>(null);
  const [selectedNode, setSelectedNode] = useState<GraphNode | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    api<{ nodes: GraphNode[]; edges: GraphEdge[] }>('/api/graph/structure').then(r => {
      setNodes(r.nodes);
      setEdges(r.edges);
    }).catch(() => {});
    api<{ mermaid: string }>('/api/graph/mermaid').then(r => setMermaid(r.mermaid)).catch(() => {});
  }, []);

  const copyMermaid = useCallback(() => {
    navigator.clipboard.writeText(mermaid);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }, [mermaid]);

  // BFS layout
  const nodePositions: Record<string, { x: number; y: number }> = {};
  const visited = new Set<string>();
  const layers: string[][] = [];

  const startNode = nodes.find(n => n.id === '__start__')?.id || nodes[0]?.id;
  if (startNode) {
    const queue: { id: string; depth: number }[] = [{ id: startNode, depth: 0 }];
    visited.add(startNode);
    while (queue.length > 0) {
      const { id, depth } = queue.shift()!;
      if (!layers[depth]) layers[depth] = [];
      layers[depth].push(id);
      for (const edge of edges) {
        if (edge.source === id && !visited.has(edge.target)) {
          visited.add(edge.target);
          queue.push({ id: edge.target, depth: depth + 1 });
        }
      }
    }
    for (const n of nodes) {
      if (!visited.has(n.id)) {
        if (!layers[layers.length]) layers.push([]);
        layers[layers.length - 1].push(n.id);
      }
    }
  }

  const svgWidth = 900;
  const layerHeight = 90;
  const svgHeight = Math.max(400, (layers.length + 1) * layerHeight);

  layers.forEach((layer, depth) => {
    const spacing = svgWidth / (layer.length + 1);
    layer.forEach((id, idx) => {
      nodePositions[id] = { x: spacing * (idx + 1), y: 60 + depth * layerHeight };
    });
  });

  // Connected nodes for hover highlight
  const getConnected = (id: string) => {
    const connected = new Set<string>();
    edges.forEach(e => {
      if (e.source === id) connected.add(e.target);
      if (e.target === id) connected.add(e.source);
    });
    return connected;
  };

  const connectedToHovered = hoveredNode ? getConnected(hoveredNode) : new Set<string>();

  const card: React.CSSProperties = {
    background: '#111', border: '1px solid #1a1a1a', borderRadius: '12px', padding: '1.25rem',
  };

  const nodeW = 140;
  const nodeH = 36;

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1.25rem' }}>
        <div>
          <h1 style={{ fontSize: '1.5rem', fontWeight: 600 }}>Agent Graph</h1>
          <p style={{ fontSize: '0.78rem', color: '#555', marginTop: '0.2rem' }}>
            {nodes.length} nodes · {edges.length} edges
          </p>
        </div>
        <div style={{ display: 'flex', gap: '0.25rem', background: '#111', borderRadius: 8, padding: '0.2rem', border: '1px solid #1a1a1a' }}>
          {(['visual', 'mermaid'] as const).map(v => (
            <button key={v} onClick={() => setView(v)} style={{
              padding: '0.4rem 1rem', borderRadius: 6, border: 'none', cursor: 'pointer',
              background: view === v ? '#f97316' : 'transparent',
              color: view === v ? '#fff' : '#666', fontSize: '0.82rem', fontWeight: view === v ? 600 : 400,
              textTransform: 'capitalize',
            }}>{v}</button>
          ))}
        </div>
      </div>

      {view === 'visual' ? (
        <div style={{ display: 'grid', gridTemplateColumns: selectedNode ? '1fr 280px' : '1fr', gap: '1rem' }}>
          {/* Graph */}
          <div style={{ ...card, overflow: 'auto', padding: '0.75rem' }}>
            <svg width={svgWidth} height={svgHeight} style={{ display: 'block', margin: '0 auto' }}>
              {/* Glow filter */}
              <defs>
                <filter id="glow">
                  <feGaussianBlur stdDeviation="3" result="coloredBlur" />
                  <feMerge><feMergeNode in="coloredBlur" /><feMergeNode in="SourceGraphic" /></feMerge>
                </filter>
                <marker id="arrow-normal" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
                  <path d="M 0 0 L 10 5 L 0 10 z" fill="#333" />
                </marker>
                <marker id="arrow-conditional" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
                  <path d="M 0 0 L 10 5 L 0 10 z" fill="#f59e0b" />
                </marker>
                <marker id="arrow-highlight" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
                  <path d="M 0 0 L 10 5 L 0 10 z" fill="#f97316" />
                </marker>
              </defs>

              {/* Edges */}
              {edges.map((e, i) => {
                const from = nodePositions[e.source];
                const to = nodePositions[e.target];
                if (!from || !to) return null;

                const isHighlighted = hoveredNode && (e.source === hoveredNode || e.target === hoveredNode);
                const isDimmed = hoveredNode && !isHighlighted;

                // Curved path for self-loops or same-layer edges
                const dx = to.x - from.x;
                const dy = to.y - from.y;
                const midX = (from.x + to.x) / 2 + (dy === 0 ? 0 : dx * 0.1);
                const midY = (from.y + to.y) / 2;

                return (
                  <path
                    key={i}
                    d={`M ${from.x} ${from.y + nodeH / 2} Q ${midX} ${midY} ${to.x} ${to.y - nodeH / 2}`}
                    fill="none"
                    stroke={isHighlighted ? '#f97316' : e.conditional ? '#f59e0b55' : '#33333388'}
                    strokeWidth={isHighlighted ? 2.5 : e.conditional ? 1.5 : 2}
                    strokeDasharray={e.conditional ? '6 3' : undefined}
                    markerEnd={isHighlighted ? 'url(#arrow-highlight)' : e.conditional ? 'url(#arrow-conditional)' : 'url(#arrow-normal)'}
                    opacity={isDimmed ? 0.15 : 1}
                    style={{ transition: 'opacity 0.2s, stroke 0.2s' }}
                  />
                );
              })}

              {/* Nodes */}
              {nodes.map(n => {
                const pos = nodePositions[n.id];
                if (!pos) return null;
                const style = getNodeStyle(n.type, n.label.toLowerCase());
                const isHovered = hoveredNode === n.id;
                const isConnected = connectedToHovered.has(n.id);
                const isDimmed = hoveredNode && !isHovered && !isConnected;

                return (
                  <g
                    key={n.id}
                    onMouseEnter={() => setHoveredNode(n.id)}
                    onMouseLeave={() => setHoveredNode(null)}
                    onClick={() => setSelectedNode(selectedNode?.id === n.id ? null : n)}
                    style={{ cursor: 'pointer', transition: 'opacity 0.2s' }}
                    opacity={isDimmed ? 0.2 : 1}
                    filter={isHovered ? 'url(#glow)' : undefined}
                  >
                    <rect
                      x={pos.x - nodeW / 2} y={pos.y - nodeH / 2}
                      width={nodeW} height={nodeH} rx={10}
                      fill={style.bg}
                      stroke={isHovered ? '#f97316' : style.border}
                      strokeWidth={isHovered ? 2 : 1}
                    />
                    <text
                      x={pos.x} y={pos.y + 4.5} textAnchor="middle"
                      fill={style.text} fontSize={11.5} fontWeight={500}
                      fontFamily='-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif'
                    >
                      {n.label}
                    </text>
                  </g>
                );
              })}
            </svg>
          </div>

          {/* Node detail panel */}
          {selectedNode && (
            <div style={card}>
              <SectionHeader title="Node Info" />
              <div style={{ marginBottom: '1rem' }}>
                <div style={{ fontSize: '1rem', fontWeight: 600, color: '#fff', marginBottom: '0.3rem' }}>{selectedNode.label}</div>
                <div style={{ fontSize: '0.72rem', color: '#555', fontFamily: 'monospace' }}>{selectedNode.id}</div>
              </div>

              <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem', fontSize: '0.78rem' }}>
                <div style={{ background: '#0a0a0a', borderRadius: 6, padding: '0.5rem 0.65rem' }}>
                  <div style={{ color: '#555', fontSize: '0.65rem', textTransform: 'uppercase', marginBottom: '0.15rem' }}>Type</div>
                  <div style={{ color: '#ccc' }}>{selectedNode.type}</div>
                </div>

                <div style={{ background: '#0a0a0a', borderRadius: 6, padding: '0.5rem 0.65rem' }}>
                  <div style={{ color: '#555', fontSize: '0.65rem', textTransform: 'uppercase', marginBottom: '0.15rem' }}>Connections In</div>
                  <div style={{ color: '#ccc' }}>
                    {edges.filter(e => e.target === selectedNode.id).map(e => {
                      const src = nodes.find(n => n.id === e.source);
                      return src?.label || e.source;
                    }).join(', ') || 'None'}
                  </div>
                </div>

                <div style={{ background: '#0a0a0a', borderRadius: 6, padding: '0.5rem 0.65rem' }}>
                  <div style={{ color: '#555', fontSize: '0.65rem', textTransform: 'uppercase', marginBottom: '0.15rem' }}>Connections Out</div>
                  <div style={{ color: '#ccc' }}>
                    {edges.filter(e => e.source === selectedNode.id).map(e => {
                      const tgt = nodes.find(n => n.id === e.target);
                      return `${tgt?.label || e.target}${e.conditional ? ' (cond)' : ''}`;
                    }).join(', ') || 'None'}
                  </div>
                </div>
              </div>

              <button
                onClick={() => setSelectedNode(null)}
                style={{
                  marginTop: '1rem', padding: '0.4rem 1rem', borderRadius: 6, border: '1px solid #222',
                  background: 'transparent', color: '#666', cursor: 'pointer', fontSize: '0.78rem', width: '100%',
                }}
              >Close</button>
            </div>
          )}
        </div>
      ) : (
        <div style={card}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '0.75rem' }}>
            <SectionHeader title="Mermaid Diagram" />
            <button onClick={copyMermaid} style={{
              padding: '0.4rem 1rem', borderRadius: 6, border: '1px solid #1a1a1a',
              background: copied ? '#166534' : '#111', color: copied ? '#4ade80' : '#888',
              cursor: 'pointer', fontSize: '0.78rem', fontWeight: 500,
            }}>{copied ? '\u2713 Copied' : 'Copy'}</button>
          </div>
          <pre style={{
            color: '#9ca3af', fontFamily: '"JetBrains Mono", "Fira Code", "SF Mono", monospace',
            fontSize: '0.82rem', whiteSpace: 'pre-wrap', lineHeight: '1.65',
            background: '#0a0a0a', padding: '1rem', borderRadius: 10, border: '1px solid #1a1a1a',
          }}>
            {mermaid || 'Loading...'}
          </pre>
        </div>
      )}

      {/* Legend */}
      {view === 'visual' && (
        <div style={{ marginTop: '1rem', display: 'flex', gap: '1.25rem', justifyContent: 'center', fontSize: '0.72rem' }}>
          {[
            { label: 'Supervisor', color: '#3b82f6' },
            { label: 'Agent', color: '#4ade80' },
            { label: 'Special', color: '#4b5563' },
            { label: 'Conditional', color: '#f59e0b', dashed: true },
          ].map(item => (
            <div key={item.label} style={{ display: 'flex', alignItems: 'center', gap: '0.35rem', color: '#666' }}>
              {item.dashed ? (
                <div style={{ width: 16, height: 0, borderTop: `2px dashed ${item.color}` }} />
              ) : (
                <div style={{ width: 10, height: 10, borderRadius: 3, background: item.color + '33', border: `1px solid ${item.color}` }} />
              )}
              {item.label}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
