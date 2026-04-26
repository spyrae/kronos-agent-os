import { useEffect, useState } from 'react';
import { api } from '../api/client';
import { SectionHeader } from '../components/Charts';
import CodeMirror from '@uiw/react-codemirror';
import { markdown } from '@codemirror/lang-markdown';

interface Skill { name: string; enabled: boolean; size: number; preview: string; }

export default function SkillsPage() {
  const [skills, setSkills] = useState<Skill[]>([]);
  const [selected, setSelected] = useState('');
  const [content, setContent] = useState('');
  const [saving, setSaving] = useState(false);
  const [toast, setToast] = useState('');
  const [showAdd, setShowAdd] = useState(false);
  const [newName, setNewName] = useState('');
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState('');

  const showToast = (msg: string) => { setToast(msg); setTimeout(() => setToast(''), 3000); };
  const showError = (msg: string) => { setError(msg); setTimeout(() => setError(''), 5000); };

  const load = async () => {
    try {
      const r = await api<{ skills: Skill[] }>('/api/skills/');
      setSkills(r.skills);
    } catch (e: any) { showError(e.message); }
  };

  useEffect(() => { load(); }, []);

  const loadSkill = async (name: string) => {
    try {
      const r = await api<{ content: string }>(`/api/skills/${name}`);
      setSelected(name);
      setContent(r.content);
    } catch (e: any) { showError(e.message); }
  };

  const saveSkill = async () => {
    setSaving(true);
    try {
      await api(`/api/skills/${selected}`, { method: 'PUT', body: JSON.stringify({ content }) });
      showToast(`${selected} saved`);
      load();
    } catch (e: any) { showError(e.message); }
    setSaving(false);
  };

  const toggleSkill = async (name: string) => {
    try {
      const r = await api<{ enabled: boolean }>(`/api/skills/${name}/toggle`, { method: 'POST' });
      showToast(`${name} ${r.enabled ? 'enabled' : 'disabled'}`);
      load();
    } catch (e: any) { showError(e.message); }
  };

  const deleteSkill = async (name: string) => {
    if (!confirm(`Delete skill "${name}" and all its files?`)) return;
    try {
      await api(`/api/skills/${name}`, { method: 'DELETE' });
      showToast(`${name} deleted`);
      if (selected === name) { setSelected(''); setContent(''); }
      load();
    } catch (e: any) { showError(e.message); }
  };

  const addSkill = async () => {
    const name = newName.trim().toLowerCase().replace(/[^a-z0-9-]/g, '-');
    if (!name) { showError('Enter a skill name'); return; }
    setCreating(true);
    try {
      await api('/api/skills/', { method: 'POST', body: JSON.stringify({ name }) });
      showToast(`Skill "${name}" created`);
      setShowAdd(false);
      setNewName('');
      await load();
      loadSkill(name);
    } catch (e: any) { showError(e.message); }
    setCreating(false);
  };

  const card: React.CSSProperties = {
    background: '#111', border: '1px solid #1a1a1a', borderRadius: '12px', padding: '1.25rem',
  };

  const enabled = skills.filter(s => s.enabled).length;

  return (
    <div>
      {/* Toast / Error */}
      {toast && <div style={{ position: 'fixed', top: '1rem', right: '1rem', padding: '0.75rem 1.5rem', borderRadius: 8, background: '#166534', color: '#fff', fontSize: '0.85rem', zIndex: 100, boxShadow: '0 4px 20px rgba(0,0,0,0.4)', border: '1px solid #4ade8033' }}>{toast}</div>}
      {error && <div style={{ position: 'fixed', top: '1rem', right: '1rem', padding: '0.75rem 1.5rem', borderRadius: 8, background: '#991b1b', color: '#fff', fontSize: '0.85rem', zIndex: 100, boxShadow: '0 4px 20px rgba(0,0,0,0.4)', border: '1px solid #ef444433' }}>{error}</div>}

      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1.25rem' }}>
        <div>
          <h1 style={{ fontSize: '1.5rem', fontWeight: 600 }}>Skills ({skills.length})</h1>
          <p style={{ fontSize: '0.78rem', color: '#555', marginTop: '0.2rem' }}>{enabled} enabled · {skills.length - enabled} disabled</p>
        </div>
        <button onClick={() => { setShowAdd(!showAdd); setNewName(''); }} style={{
          padding: '0.45rem 1.1rem', borderRadius: 8, border: 'none', cursor: 'pointer',
          background: showAdd ? '#333' : '#f97316', color: '#fff', fontSize: '0.82rem', fontWeight: 600,
        }}>{showAdd ? 'Cancel' : '+ New Skill'}</button>
      </div>

      {/* Add form */}
      {showAdd && (
        <div style={{ ...card, marginBottom: '1rem', borderColor: '#f97316' }}>
          <SectionHeader title="Create Skill" />
          <div style={{ display: 'flex', gap: '0.5rem' }}>
            <input
              placeholder="skill-name (lowercase, hyphens)"
              value={newName} onChange={e => setNewName(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter') addSkill(); }}
              autoFocus
              style={{
                flex: 1, padding: '0.55rem 0.85rem', borderRadius: 8, border: '1px solid #222',
                background: '#0a0a0a', color: '#fff', fontSize: '0.85rem', outline: 'none',
              }}
            />
            <button onClick={addSkill} disabled={creating} style={{
              padding: '0.55rem 1.5rem', borderRadius: 8, border: 'none', cursor: creating ? 'wait' : 'pointer',
              background: creating ? '#333' : '#f97316', color: '#fff', fontSize: '0.85rem', fontWeight: 600,
            }}>{creating ? 'Creating...' : 'Create'}</button>
          </div>
        </div>
      )}

      <div style={{ display: 'flex', gap: '1rem' }}>
        {/* Skill list */}
        <div style={{ width: '230px', flexShrink: 0 }}>
          <div style={card}>
            <SectionHeader title="Skills" />
            <div style={{ maxHeight: 'calc(100vh - 260px)', overflow: 'auto', display: 'flex', flexDirection: 'column', gap: '0.2rem' }}>
              {skills.map(s => (
                <div key={s.name} style={{
                  display: 'flex', justifyContent: 'space-between', alignItems: 'center',
                  padding: '0.5rem 0.6rem', borderRadius: 8, cursor: 'pointer',
                  background: selected === s.name ? '#1a1a2e' : 'transparent',
                  border: `1px solid ${selected === s.name ? '#2563eb33' : 'transparent'}`,
                  opacity: s.enabled ? 1 : 0.5,
                }} onClick={() => loadSkill(s.name)}>
                  <span style={{ flex: 1, color: selected === s.name ? '#fff' : '#999', fontSize: '0.82rem', fontWeight: selected === s.name ? 500 : 400 }}>
                    {s.name}
                  </span>
                  <div style={{ display: 'flex', gap: '0.2rem', flexShrink: 0 }}>
                    <button onClick={(e) => { e.stopPropagation(); toggleSkill(s.name); }} style={{
                      padding: '0.12rem 0.4rem', borderRadius: 4, border: 'none', cursor: 'pointer', fontSize: '0.6rem', fontWeight: 600,
                      background: s.enabled ? '#166534' : '#333', color: '#fff',
                    }}>{s.enabled ? 'ON' : 'OFF'}</button>
                    <button onClick={(e) => { e.stopPropagation(); deleteSkill(s.name); }} style={{
                      padding: '0.12rem 0.35rem', borderRadius: 4, border: 'none', cursor: 'pointer', fontSize: '0.6rem',
                      background: 'transparent', color: '#555',
                    }}>{'\u2715'}</button>
                  </div>
                </div>
              ))}
              {skills.length === 0 && <div style={{ padding: '1rem', textAlign: 'center', color: '#555', fontSize: '0.82rem' }}>No skills yet</div>}
            </div>
          </div>
        </div>

        {/* Editor */}
        <div style={{ flex: 1 }}>
          {selected ? (
            <div style={card}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '0.75rem' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                  <span style={{ fontSize: '0.78rem', color: '#14b8a6', fontFamily: 'monospace' }}>skills/</span>
                  <span style={{ fontSize: '0.85rem', color: '#fff', fontWeight: 500 }}>{selected}/SKILL.md</span>
                </div>
                <button onClick={saveSkill} disabled={saving} style={{
                  padding: '0.45rem 1.25rem', borderRadius: 8, border: 'none', cursor: 'pointer',
                  background: saving ? '#333' : '#f97316', color: '#fff', fontSize: '0.82rem', fontWeight: 600,
                }}>{saving ? 'Saving...' : 'Save'}</button>
              </div>
              <div style={{ border: '1px solid #1a1a1a', borderRadius: '10px', overflow: 'hidden' }}>
                <CodeMirror value={content} onChange={setContent} extensions={[markdown()]} theme="dark" height="calc(100vh - 240px)" />
              </div>
            </div>
          ) : (
            <div style={{ ...card, display: 'flex', alignItems: 'center', justifyContent: 'center', height: 'calc(100vh - 180px)' }}>
              <div style={{ textAlign: 'center', color: '#555' }}>
                <div style={{ fontSize: '2rem', marginBottom: '0.5rem' }}>{'\u2699'}</div>
                <div style={{ fontSize: '0.95rem' }}>Select a skill or create a new one</div>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
