import { FormEvent, useCallback, useEffect, useState } from "react";
import { api, fmtTime, Store } from "../api";

const LABEL: Record<Store["status"], string> = { provisioning: "Provisioning", ready: "Ready", failed: "Failed", deleting: "Deleting" };

export default function Stores() {
  const [stores, setStores] = useState<Store[]>([]);
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [error, setError] = useState("");

  const load = useCallback(() => api<Store[]>("/stores").then(setStores, (e) => setError(e.message)), []);
  useEffect(() => {
    load();
    const t = setInterval(load, 4000);
    return () => clearInterval(t);
  }, [load]);

  const create = async (e: FormEvent) => {
    e.preventDefault();
    setError("");
    try {
      await api("/stores", { body: { name, slug } });
      setName("");
      setSlug("");
      load();
    } catch (err) {
      setError((err as Error).message);
    }
  };
  const remove = async (s: Store) => {
    if (!confirm(`Delete store ${s.name}? Its namespace, database, volumes, secrets and MCP server are removed.`)) return;
    try {
      await api(`/stores/${s.id}`, { method: "DELETE" });
      load();
    } catch (err) {
      setError((err as Error).message);
    }
  };

  return (
    <>
      <header className="page-head">
        <div>
          <h1>Stores</h1>
          <p className="muted">Each store gets its own namespace, database, volumes, Ingress host and WooCommerce MCP server, registered in the gateway automatically.</p>
        </div>
      </header>
      <form className="card row gap" onSubmit={create}>
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Store name" required />
        <input value={slug} onChange={(e) => setSlug(e.target.value.toLowerCase())} placeholder="slug, e.g. berlin" pattern="[a-z][a-z0-9]{1,11}" title="2-12 lowercase letters or digits" required />
        <button className="primary">Create store</button>
      </form>
      {error && <div className="error">{error}</div>}
      <table className="card table">
        <thead>
          <tr>
            <th>Store</th>
            <th>Status</th>
            <th>URL</th>
            <th>Tools</th>
            <th>Created</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {stores.map((s) => (
            <tr key={s.id}>
              <td>
                {s.name}
                <div className="muted small">
                  {s.namespace} · {s.owner_email}
                </div>
              </td>
              <td>
                <span className={`badge store-${s.status}`}>{LABEL[s.status]}</span>
                {s.status === "provisioning" && s.step && <div className="muted small">step: {s.step}</div>}
                {s.status === "deleting" && s.step && <div className="muted small">step: {s.step}</div>}
                {s.status_reason && <div className={`small ${s.status === "failed" ? "error" : "muted"}`}>{s.status_reason}</div>}
              </td>
              <td>{s.status === "ready" ? <a href={s.url} target="_blank" rel="noreferrer">{s.url}</a> : <span className="muted">{s.url}</span>}</td>
              <td>
                {s.mcp_server ? <code>{s.mcp_server}__*</code> : "—"}
                {s.mcp_status && <div className="muted small">{s.mcp_status}</div>}
              </td>
              <td className="small">{fmtTime(s.created_at)}</td>
              <td className="row gap">
                {s.status === "failed" && <button onClick={() => api(`/stores/${s.id}/retry`, { method: "POST" }).then(load, (e) => setError(e.message))}>Retry</button>}
                {s.status !== "deleting" && (
                  <button className="danger" onClick={() => remove(s)}>
                    Delete
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}
