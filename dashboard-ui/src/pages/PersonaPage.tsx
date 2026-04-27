import { useEffect, useState } from 'react';
import { api } from '../api/client';
import { SectionHeader } from '../components/Charts';
import CodeMirror from '@uiw/react-codemirror';
import { markdown } from '@codemirror/lang-markdown';

interface PersonaFile { name: string; size: number; preview: string; }

const FILE_ICONS: Record<string, string> = {
  IDENTITY: '\u{1F9E0}', SOUL: '\u2728', USER: '\u{1F464}', STYLE: '\u{1F3A8}',
  RULES: '\u{1F4DC}', CONTEXT: '\u{1F30D}', MEMORY: '\u{1F4BE}',
};

export default function PersonaPage() {
  const [files, setFiles] = useState<PersonaFile[]>([]);
  const [selected, setSelected] = useState<string>('');
  const [content, setContent] = useState('');
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    api<{ files: PersonaFile[] }>('/api/persona/files').then(r => setFiles(r.files)).catch(() => {});
  }, []);

  const loadFile = async (name: string) => {
    const r = await api<{ content: string }>(`/api/persona/files/${name}`);
    setSelected(name);
    setContent(r.content);
    setSaved(false);
  };

  const saveFile = async () => {
    setSaving(true);
    try {
      await api(`/api/persona/files/${selected}`, { method: 'PUT', body: JSON.stringify({ content }) });
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } catch { /* */ }
    setSaving(false);
  };

  const card: React.CSSProperties = {
    background: '#111', border: '1px solid #1a1a1a', borderRadius: '12px', padding: '1.25rem',
  };

  return (
    <div>
      <h1 style={{ fontSize: '1.5rem', fontWeight: 600, marginBottom: '1.25rem' }}>Persona Editor</h1>

      <div style={{ display: 'flex', gap: '1rem' }}>
        {/* File list */}
        <div style={{ width: '220px', flexShrink: 0 }}>
          <div style={card}>
            <SectionHeader title="Workspace Files" />
            <div style={{ display: 'flex', flexDirection: 'column', gap: '0.25rem' }}>
              {files.map(f => {
                const baseName = f.name.replace(/\.md$/, '').toUpperCase();
                return (
                  <div
                    key={f.name}
                    onClick={() => loadFile(f.name)}
                    style={{
                      padding: '0.55rem 0.65rem', cursor: 'pointer', borderRadius: 8,
                      background: selected === f.name ? '#1a1a2e' : 'transparent',
                      border: `1px solid ${selected === f.name ? '#2563eb33' : 'transparent'}`,
                      display: 'flex', alignItems: 'center', gap: '0.5rem',
                    }}
                  >
                    <span style={{ fontSize: '0.85rem' }}>{FILE_ICONS[baseName] || '\u{1F4C4}'}</span>
                    <div>
                      <div style={{
                        fontSize: '0.82rem', fontWeight: selected === f.name ? 500 : 400,
                        color: selected === f.name ? '#fff' : '#999',
                      }}>{f.name}</div>
                      <div style={{ fontSize: '0.62rem', color: '#444' }}>{f.size} chars</div>
                    </div>
                  </div>
                );
              })}
              {files.length === 0 && (
                <div style={{ padding: '1rem', textAlign: 'center', color: '#555', fontSize: '0.82rem' }}>No workspace files</div>
              )}
            </div>
          </div>
        </div>

        {/* Editor */}
        <div style={{ flex: 1 }}>
          {selected ? (
            <div style={card}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '0.75rem' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                  <span style={{ fontSize: '0.78rem', color: '#f97316', fontFamily: 'monospace' }}>workspace/</span>
                  <span style={{ fontSize: '0.85rem', color: '#fff', fontWeight: 500 }}>{selected}</span>
                </div>
                <button
                  onClick={saveFile}
                  disabled={saving}
                  style={{
                    padding: '0.45rem 1.25rem', borderRadius: '8px', border: 'none', cursor: 'pointer',
                    background: saved ? '#166534' : '#f97316', color: '#fff',
                    fontSize: '0.82rem', fontWeight: 600,
                  }}
                >{saving ? 'Saving...' : saved ? '\u2713 Saved' : 'Save'}</button>
              </div>
              <div style={{ border: '1px solid #1a1a1a', borderRadius: '10px', overflow: 'hidden' }}>
                <CodeMirror
                  value={content}
                  onChange={setContent}
                  extensions={[markdown()]}
                  theme="dark"
                  height="calc(100vh - 220px)"
                  style={{ fontSize: '0.88rem' }}
                />
              </div>
            </div>
          ) : (
            <div style={{ ...card, display: 'flex', alignItems: 'center', justifyContent: 'center', height: 'calc(100vh - 150px)' }}>
              <div style={{ textAlign: 'center', color: '#555' }}>
                <div style={{ fontSize: '2rem', marginBottom: '0.5rem' }}>{'\u{1F4DD}'}</div>
                <div style={{ fontSize: '0.95rem' }}>Select a file to edit</div>
                <div style={{ fontSize: '0.75rem', color: '#444', marginTop: '0.3rem' }}>Workspace files define Kronos Agent OS personality</div>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
