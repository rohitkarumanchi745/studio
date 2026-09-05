const BASE = "/api";

export function getToken() {
  return localStorage.getItem("studio_token");
}

export function setSession(token, user) {
  localStorage.setItem("studio_token", token);
  localStorage.setItem("studio_user", JSON.stringify(user));
}

export function getUser() {
  try {
    return JSON.parse(localStorage.getItem("studio_user"));
  } catch {
    return null;
  }
}

export function clearSession() {
  localStorage.removeItem("studio_token");
  localStorage.removeItem("studio_user");
}

// SSO handoff: the callback redirect carries a single-use code, never the JWT.
// POST keeps the token out of the URL, history and Referer on the way back too.
// Throws on an unknown/expired/replayed code (the backend answers 400).
export function exchangeSsoCode(code) {
  return api("/auth/sso/exchange", { method: "POST", body: JSON.stringify({ code }) });
}

export async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  const res = await fetch(BASE + path, { ...options, headers });
  // Expired/invalid session → back to login. Auth endpoints are exempt so a
  // wrong password shows its error instead of silently reloading the page.
  if (res.status === 401 && !path.startsWith("/auth/")) {
    clearSession();
    window.location.reload();
    return;
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `Request failed (${res.status})`);
  return data;
}
