export const API_BASE = 'http://127.0.0.1:8001';

export function getAuthToken() {
  return localStorage.getItem('auth_token') || localStorage.getItem('api_key');
}

export function getAuthHeaders() {
  const token = localStorage.getItem('auth_token');
  const apiKey = localStorage.getItem('api_key');
  const headers = { 'Content-Type': 'application/json' };
  
  if (token) {
    headers['Authorization'] = `Bearer ${token}`;
  } else if (apiKey) {
    headers['X-API-Key'] = apiKey;
  }
  
  return headers;
}

export async function getSseQueryAsync(extra) {
  const suffix = extra ? `&${extra}` : '';
  try {
    const res = await apiCall('/v1/auth/sse-ticket', { method: 'POST' });
    if (res && res.ticket) {
      return `?ticket=${encodeURIComponent(res.ticket)}${suffix}`;
    }
  } catch {}
  
  const token = localStorage.getItem('auth_token');
  const apiKey = localStorage.getItem('api_key');
  if (token) return `?api_key=${encodeURIComponent(token)}${suffix}`;
  if (apiKey) return `?api_key=${encodeURIComponent(apiKey)}${suffix}`;
  return '';
}

export async function apiCall(path, options = {}) {
  const url = `${API_BASE}${path}`;
  const fetchOptions = {
    ...options,
    headers: {
      ...getAuthHeaders(),
      ...(options.headers || {})
    }
  };

  if (options.body instanceof FormData) {
    delete fetchOptions.headers['Content-Type'];
  }

  const response = await fetch(url, fetchOptions);

  if (response.status === 401) {
    localStorage.removeItem('auth_token');
    localStorage.removeItem('api_key');
    window.dispatchEvent(new Event('auth-expired'));
    throw new Error('Unauthorized — please log in again.');
  }

  let data;
  const contentType = response.headers.get('content-type') || '';
  if (contentType.includes('application/json')) {
    data = await response.json();
  } else {
    data = await response.text();
  }

  if (!response.ok) {
    let msg = `API error ${response.status}`;
    if (data && data.error) {
      msg = data.error.message || JSON.stringify(data.error);
    } else if (data && data.detail) {
      if (Array.isArray(data.detail)) {
        const parts = data.detail.map(d => d && d.msg ? String(d.msg).replace(/^Value error,\s*/, '') : null).filter(Boolean);
        msg = parts.length ? parts.join('; ') : JSON.stringify(data.detail);
      } else {
        msg = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail);
      }
    } else if (typeof data === 'string' && data) {
      msg = data;
    }
    throw new Error(msg);
  }

  return data;
}

export async function login(username, password, apiKeyInput) {
  if (apiKeyInput) {
    localStorage.setItem('api_key', apiKeyInput.trim());
    return true;
  }
  
  const res = await fetch(`${API_BASE}/v1/auth/token`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password })
  });

  if (!res.ok) {
    let msg = 'Login failed';
    try {
      const data = await res.json();
      msg = data.detail || data.error?.message || msg;
    } catch {}
    throw new Error(msg);
  }
  
  const data = await res.json();
  localStorage.setItem('auth_token', data.access_token || data.token);
  return true;
}

export async function signup(username, password) {
  const res = await fetch(`${API_BASE}/v1/auth/signup`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password })
  });

  if (!res.ok) {
    let msg = 'Signup failed';
    try {
      const data = await res.json();
      msg = data.detail || data.error?.message || msg;
    } catch {}
    throw new Error(msg);
  }

  const data = await res.json();
  localStorage.setItem('auth_token', data.access_token || data.token);
  localStorage.removeItem('api_key');
  return true;
}

export function logout() {
  localStorage.removeItem('auth_token');
  localStorage.removeItem('api_key');
}
