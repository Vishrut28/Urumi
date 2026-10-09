// Thin fetch wrapper. The session is an HttpOnly cookie; the custom header is the CSRF guard.

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export async function api<T = unknown>(path: string, init: { method?: string; body?: unknown } = {}): Promise<T> {
  const res = await fetch(`/api${path}`, {
    method: init.method ?? (init.body ? "POST" : "GET"),
    headers: { "Content-Type": "application/json", "X-Requested-With": "dashboard" },
    body: init.body ? JSON.stringify(init.body) : undefined,
    credentials: "same-origin",
  });
  if (!res.ok) {
    let message = res.statusText;
    try {
      const data = await res.json();
      message = typeof data.detail === "string" ? data.detail : Array.isArray(data.detail) ? data.detail.map((d: { msg: string }) => d.msg).join("; ") : message;
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, message);
  }
  return res.json() as Promise<T>;
}

export type Me = { id: number; email: string; name: string; role: "admin" | "member" };

export type Tool = { name: string; upstream: string; description: string; admin_only: boolean; read_only: boolean };

export type Server = {
  id: number;
  slug: string;
  name: string;
  enabled: boolean;
  status: "connected" | "unreachable" | "disabled" | "unknown";
  status_reason: string | null;
  checked_at: string | null;
  tools: Tool[];
  managed_by: string | null;
  url?: string;
  auth_type?: "none" | "bearer" | "header";
  auth_header?: string | null;
  has_credential?: boolean;
  timeout_seconds?: number;
  admin_only_tools?: string[];
};

export type ApiKey = {
  id: number;
  name: string;
  prefix: string;
  user_id: number;
  user_email: string | null;
  created_at: string;
  last_used_at: string | null;
  revoked_at: string | null;
  key?: string;
};

export type Member = Me & { active: boolean; created_at: string; active_keys: number };

export type AuditRow = {
  id: number;
  ts: string;
  user_email: string | null;
  source: string;
  api_key_prefix: string | null;
  server: string | null;
  tool: string;
  duration_ms: number;
  outcome: string;
  error: string | null;
};

export type Store = {
  id: number;
  slug: string;
  name: string;
  status: "provisioning" | "ready" | "failed" | "deleting";
  status_reason: string | null;
  url: string;
  namespace: string;
  created_at: string;
  owner_email: string | null;
  mcp_server: string | null;
};

export const fmtTime = (iso: string | null) => (iso ? new Date(iso).toLocaleString() : "—");
