import { FormEvent, useCallback, useEffect, useState } from "react";
import { api, fmtTime, Server } from "../api";
import { useSession } from "../main";

const STATUS_LABEL: Record<Server["status"], string> = { connected: "Connected", unreachable: "Unreachable", disabled: "Disabled", unknown: "Checking…" };

type Form = {
  slug: string;
  name: string;
  url: string;
  auth_type: "none" | "bearer" | "header";
  auth_header: string;
  credential: string;
  timeout_seconds: number;
};
const EMPTY: Form = { slug: "", name: "", url: "", auth_type: "none", auth_header: "", credential: "", timeout_seconds: 20 };

export default function Servers() {
  const { me } = useSession();
  const admin = me.role === "admin";
  const [servers, setServers] = useState<Server[]>([]);
  const [error, setError] = useState("");
  const [editing, setEditing] = useState<Server | "new" | null>(null);
  const [open, setOpen] = useState<Record<number, boolean>>({});

  const load = useCallback(() => api<Server[]>("/servers").then(setServers, (e) => setError(e.message)), []);
  useEffect(() => {
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, [load]);

  const act = async (fn: () => Promise<unknown>) => {
    setError("");
    try {
      await fn();
      await load();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const toggleAdminOnly = (s: Server, tool: string) =>
    act(() => {
      const current = new Set(s.admin_only_tools ?? []);
      current.has(tool) ? current.delete(tool) : current.add(tool);
      return api(`/servers/${s.id}`, { method: "PATCH", body: { admin_only_tools: [...current] } });
    });

  return (
    <>
      <header className="page-head">
        <div>
          <h1>MCP servers</h1>
          <p className="muted">Every enabled, connected server's tools are available through one gateway endpoint, named <code>server__tool</code>.</p>
        </div>
        {admin && (
          <button className="primary" onClick={() => setEditing("new")}>
            Add server
          </button>
        )}
      </header>
      {error && <div className="error">{error}</div>}
      {editing && <ServerForm server={editing === "new" ? null : editing} onDone={() => (setEditing(null), load())} />}
      <div className="stack">
        {servers.map((s) => (
          <section className="card" key={s.id}>
            <div className="row between">
              <div>
                <div className="row gap">
                  <h2>{s.name}</h2>
                  <span className={`badge status-${s.status}`}>{STATUS_LABEL[s.status]}</span>
                  {s.managed_by?.startsWith("store:") && <span className="badge">provisioned store</span>}
                </div>
                <div className="muted small">
                  prefix <code>{s.slug}__</code>
                  {s.url && (
                    <>
                      {" "}· <code>{s.url}</code> · auth: {s.auth_type}
                      {s.auth_type === "header" && ` (${s.auth_header})`}
                      {s.has_credential && " · credential stored encrypted"}
                    </>
                  )}
                  {" "}· checked {fmtTime(s.checked_at)}
                </div>
                {s.status === "unreachable" && s.status_reason && <div className="error small">Why: {s.status_reason}</div>}
              </div>
              {admin && (
                <div className="row gap">
                  <button onClick={() => act(() => api(`/servers/${s.id}/refresh`, { method: "POST" }))}>Check now</button>
                  <button onClick={() => setEditing(s)}>Edit</button>
                  <button onClick={() => act(() => api(`/servers/${s.id}`, { method: "PATCH", body: { enabled: !s.enabled } }))}>{s.enabled ? "Disable" : "Enable"}</button>
                  {!s.managed_by?.startsWith("store:") && (
                    <button className="danger" onClick={() => confirm(`Remove ${s.name}?`) && act(() => api(`/servers/${s.id}`, { method: "DELETE" }))}>
                      Remove
                    </button>
                  )}
                </div>
              )}
            </div>
            <button className="link" onClick={() => setOpen({ ...open, [s.id]: !open[s.id] })}>
              {open[s.id] ? "Hide" : "Show"} {s.tools.length} tools
            </button>
            {open[s.id] && (
              <table className="tools">
                <thead>
                  <tr>
                    <th>Tool</th>
                    <th>Description</th>
                    <th>Kind</th>
                    {admin && <th title="Hidden from members' tool lists and rejected if they call it">Admins only</th>}
                  </tr>
                </thead>
                <tbody>
                  {s.tools.map((t) => (
                    <tr key={t.name}>
                      <td>
                        <code>{t.name}</code>
                      </td>
                      <td className="small">{t.description}</td>
                      <td className="small">{t.read_only ? "read" : "write"}</td>
                      {admin && (
                        <td>
                          <input type="checkbox" checked={t.admin_only} onChange={() => toggleAdminOnly(s, t.upstream)} />
                        </td>
                      )}
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>
        ))}
      </div>
    </>
  );
}

function ServerForm({ server, onDone }: { server: Server | null; onDone: () => void }) {
  const [f, setF] = useState<Form>(
    server
      ? { slug: server.slug, name: server.name, url: server.url ?? "", auth_type: server.auth_type ?? "none", auth_header: server.auth_header ?? "", credential: "", timeout_seconds: server.timeout_seconds ?? 20 }
      : EMPTY,
  );
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const set = (k: keyof Form) => (e: { target: { value: string } }) => setF({ ...f, [k]: k === "timeout_seconds" ? Number(e.target.value) : e.target.value });

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    const body: Record<string, unknown> = { name: f.name, url: f.url, auth_type: f.auth_type, timeout_seconds: f.timeout_seconds, auth_header: f.auth_type === "header" ? f.auth_header : null };
    if (f.credential) body.credential = f.credential;
    try {
      if (server) await api(`/servers/${server.id}`, { method: "PATCH", body });
      else await api("/servers", { body: { ...body, slug: f.slug } });
      onDone();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="card form" onSubmit={submit}>
      <h2>{server ? `Edit ${server.name}` : "Add an MCP server"}</h2>
      <div className="grid2">
        <label>
          Display name
          <input value={f.name} onChange={set("name")} required />
        </label>
        <label>
          Tool prefix (slug)
          <input value={f.slug} onChange={set("slug")} disabled={!!server} pattern="[a-z]([a-z0-9-]{0,22}[a-z0-9])?" placeholder="e.g. crm" required />
        </label>
        <label className="span2">
          Streamable HTTP URL
          <input value={f.url} onChange={set("url")} placeholder="https://mcp.example.com/mcp" required />
        </label>
        <label>
          Credential type
          <select value={f.auth_type} onChange={set("auth_type")}>
            <option value="none">None</option>
            <option value="bearer">Bearer token</option>
            <option value="header">Custom header</option>
          </select>
        </label>
        {f.auth_type === "header" && (
          <label>
            Header name
            <input value={f.auth_header} onChange={set("auth_header")} placeholder="X-API-Key" required />
          </label>
        )}
        {f.auth_type !== "none" && (
          <label className="span2">
            Credential {server?.has_credential && <span className="muted">(stored; leave empty to keep it)</span>}
            <input type="password" autoComplete="off" value={f.credential} onChange={set("credential")} required={!server?.has_credential} />
          </label>
        )}
        <label>
          Call timeout (seconds)
          <input type="number" min={1} max={120} value={f.timeout_seconds} onChange={set("timeout_seconds")} />
        </label>
      </div>
      <p className="muted small">The credential is encrypted at rest and only ever sent from the gateway to this server. Members and MCP clients never see it.</p>
      {error && <div className="error">{error}</div>}
      <div className="row gap">
        <button className="primary" disabled={busy}>
          {busy ? "Connecting…" : server ? "Save" : "Add and connect"}
        </button>
        <button type="button" onClick={onDone}>
          Cancel
        </button>
      </div>
    </form>
  );
}
