import { useCallback, useEffect, useState } from "react";
import { api, AuditRow, fmtTime } from "../api";
import { useSession } from "../main";

type Stat = { server: string; calls: number; errors: number; avg_ms: number };

export default function Audit() {
  const { me } = useSession();
  const [rows, setRows] = useState<AuditRow[]>([]);
  const [stats, setStats] = useState<Stat[]>([]);
  const [server, setServer] = useState("");
  const [outcome, setOutcome] = useState("");

  const load = useCallback(() => {
    const q = new URLSearchParams();
    if (server) q.set("server", server);
    if (outcome) q.set("outcome", outcome);
    api<AuditRow[]>(`/audit?${q}`).then(setRows);
    api<{ servers: Stat[] }>("/stats").then((s) => setStats(s.servers));
  }, [server, outcome]);
  useEffect(() => {
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, [load]);

  return (
    <>
      <header className="page-head">
        <div>
          <h1>Audit log</h1>
          <p className="muted">{me.role === "admin" ? "Every tool call through the gateway, by every member." : "Your tool calls through the gateway."}</p>
        </div>
      </header>
      <div className="stats">
        {stats.map((s) => (
          <div className="card stat" key={s.server}>
            <div className="muted small">{s.server} · 24 h</div>
            <div className="big">{s.calls}</div>
            <div className="small">
              {s.errors} failed ({s.calls ? Math.round((100 * s.errors) / s.calls) : 0}%) · avg {Math.round(s.avg_ms)} ms
            </div>
          </div>
        ))}
      </div>
      <div className="row gap">
        <input placeholder="filter by server slug" value={server} onChange={(e) => setServer(e.target.value)} />
        <select value={outcome} onChange={(e) => setOutcome(e.target.value)}>
          <option value="">all outcomes</option>
          {["ok", "error", "timeout", "unreachable", "denied", "rejected"].map((o) => (
            <option key={o}>{o}</option>
          ))}
        </select>
      </div>
      <table className="card table audit">
        <thead>
          <tr>
            <th>When</th>
            <th>Who</th>
            <th>Via</th>
            <th>Server</th>
            <th>Tool</th>
            <th>Duration</th>
            <th>Outcome</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id}>
              <td className="small">{fmtTime(r.ts)}</td>
              <td>{r.user_email ?? "—"}</td>
              <td className="small">
                {r.source}
                {r.api_key_prefix && <div className="muted">{r.api_key_prefix}…</div>}
              </td>
              <td>{r.server ?? "—"}</td>
              <td>
                <code>{r.tool}</code>
              </td>
              <td>{r.duration_ms} ms</td>
              <td>
                <span className={`badge outcome-${r.outcome}`}>{r.outcome}</span>
                {r.error && <div className="muted small">{r.error}</div>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}
