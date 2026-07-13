const API_BASE = import.meta.env.VITE_API_URL || '';

// Auth is a same-origin HttpOnly session cookie: the browser attaches it
// automatically, so requests just need `credentials: 'include'`. The token is
// never held in JS/localStorage (an XSS can't read an HttpOnly cookie).

export async function api<T = any>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      ...(options?.headers || {}),
    },
  });
  if (res.status === 401) {
    // Session expired/invalid — reload lands on the login screen.
    window.location.reload();
    throw new Error('Unauthorized');
  }
  if (!res.ok) throw new Error(`${res.status}: ${await res.text()}`);
  return res.json();
}

export async function login(username: string, password: string): Promise<void> {
  const res = await fetch(`${API_BASE}/api/auth/login`, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  });
  if (!res.ok) {
    throw new Error(
      res.status === 429 ? 'Too many attempts. Try again later.' : 'Invalid credentials',
    );
  }
}

export async function logout(): Promise<void> {
  try {
    await fetch(`${API_BASE}/api/auth/logout`, { method: 'POST', credentials: 'include' });
  } catch {
    // Best-effort: the subsequent reload lands on the login screen regardless.
  }
}

export async function checkAuth(): Promise<boolean> {
  try {
    const res = await fetch(`${API_BASE}/api/auth/me`, { credentials: 'include' });
    return res.ok;
  } catch {
    return false;
  }
}
