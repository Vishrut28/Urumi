import { FormEvent, useCallback, useEffect, useState } from "react";
import { api, ApiKey, fmtTime } from "../api";
import { useSession } from "../main";

export default function Keys() {
  const { me } = useSession();
  const admin = me.role === "admin";
  const [keys, setKeys] = useState<ApiKey[]>([]);
  const [all, setAll] = useState(false);
  const [name, setName] = useState("");
  const [fresh, setFresh] = useState<ApiKey | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(() => api<ApiKey[]>(`/keys${all ? "?all=true" : ""}`).then(setKeys, (e) => setError(e.message)), [all]);
  useEffect(() => {
    load();
  }, [load]);

  const create = async (e: FormEvent) => {
    e.preventDefault();
    try {
      setFresh(await api<ApiKey>("/keys", { body: { name } }));
      setName("");
      load();
    } catch (err) {
      setError((err as Error).message);
    }
  };
  const revoke = async (k: ApiKey) => {
    if (!confirm(`Revoke ${k.prefix}…? Clients using it stop working immediately.`)) return;
    await api(`/keys/${k.id}/revoke`, { method: "POST" });
    load();
  };

  return (
    <>
      <header className="page-head">
        <div>
          <h1>API keys</h1>
          <p className="muted">Keys let an MCP client act as you through the gateway. They are stored hashed and shown only once.</p>
        </div>
      </header>
      <form className="card row gap" onSubmit={create}>
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Key name, e.g. laptop-claude-code" required />
        <button className="primary">Generate key</button>
      </form>
      {fresh?.key && (
        <div className="card notice">
          <b>Copy your new key now. It will not be shown again.</b>
          <pre className="copy">{fresh.key}</pre>
          <button onClick={() => setFresh(null)}>Done</button>
        </div>
      )}
      {error && <div className="error">{error}</div>}
      {admin && (
        <label className="row gap small">
          <input type="checkbox" checked={all} onChange={(e) => setAll(e.target.checked)} /> Show every member's keys
        </label>
      )}
      <table className="card table">
        <thead>
          <tr>
            <th>Name</th>
            <th>Key</th>
            {all && <th>Owner</th>}
            <th>Created</th>
            <th>Last used</th>
            <th>Status</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {keys.map((k) => (
            <tr key={k.id} className={k.revoked_at ? "dim" : ""}>
              <td>{k.name}</td>
              <td>
                <code>{k.prefix}…</code>
              </td>
              {all && <td>{k.user_email}</td>}
              <td>{fmtTime(k.created_at)}</td>
              <td>{fmtTime(k.last_used_at)}</td>
              <td>{k.revoked_at ? `revoked ${fmtTime(k.revoked_at)}` : <span className="badge status-connected">active</span>}</td>
              <td>{!k.revoked_at && <button className="danger" onClick={() => revoke(k)}>Revoke</button>}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}
