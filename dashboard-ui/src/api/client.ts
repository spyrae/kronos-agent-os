const API_BASE = import.meta.env.VITE_API_URL || '';

let token = localStorage.getItem('kronos_token') || '';

export function setToken(t: string) {
  token = t;
  localStorage.setItem('kronos_token', t);
}

export function getToken(): string {
  return token;
}

export async function api<T = any>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${token}`,
      ...(options?.headers || {}),
    },
  });
  if (res.status === 401) {
    localStorage.removeItem('kronos_token');
    window.location.reload();
    throw new Error('Unauthorized');
  }
  if (!res.ok) throw new Error(`${res.status}: ${await res.text()}`);
  return res.json();
}
