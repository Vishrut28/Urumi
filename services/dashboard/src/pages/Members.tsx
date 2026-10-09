import { FormEvent, useCallback, useEffect, useState } from "react";
import { api, ApiKey, fmtTime, Member } from "../api";
import { useSession } from "../main";

type Invite = { id: number; email: string; role: string; expires_at: string; expired: boolean };

export default function Members() {
  const { me } = useSession();
  const [members, setMembers] = useState<Member[]>([]);
  const [invites, setInvites] = useState<Invite[]>([]);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("member");
  const [link, setLink] = useState("");
  const [issued, setIssued] = useState<ApiKey | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(() => {
    api<Member[]>("/members").then(setMembers);
    api<Invite[]>("/invites").then(setInvites);
  }, []);
  useEffect(load, [load]);

  const act = async (fn: () => Promise<unknown>) => {
    setError("");
    try {
      await fn();
      load();
    } catch (e) {
      setError((e as Error).message);
    }
  };
  const invite = (e: FormEvent) => {
    e.preventDefault();
    act(async () => {
      const r = await api<{ link: string }>("/invites", { body: { email, role } });
      setLink(r.link);
      setEmail("");
    });
  };
  const issue = (m: Member) => act(async () => setIssued(await api<ApiKey>("/keys", { body: { name: `issued by ${me.email}`, user_id: m.id } })));
  const revokeAll = (m: Member) =>
    confirm(`Revoke every active key of ${m.email}?`) &&
    act(async () => {
      const keys = await api<ApiKey[]>("/keys?all=true");
      await Promise.all(keys.filter((k) => k.user_id === m.id && !k.revoked_at).map((k) => api(`/keys/${k.id}/revoke`, { method: "POST" })));
    });

  return (
    <>
      <header className="page-head">
        <div>
          <h1>Members</h1>
          <p className="muted">Admins manage servers and members. Members use tools through the gateway and the chat.</p>
        </div>
      </header>
      <form className="card row gap" onSubmit={invite}>
        <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="teammate@example.com" required />
        <select value={role} onChange={(e) => setRole(e.target.value)}>
          <option value="member">member</option>
          <option value="admin">admin</option>
        </select>
        <button className="primary">Invite</button>
      </form>
      {link && (
        <div className="card notice">
          <b>Send this invite link to your teammate (valid for 7 days, shown once):</b>
          <pre className="copy">{link}</pre>
          <button onClick={() => setLink("")}>Done</button>
        </div>
      )}
      {issued?.key && (
        <div className="card notice">
          <b>Key issued for {issued.user_email}. Hand it over securely; it will not be shown again.</b>
          <pre className="copy">{issued.key}</pre>
          <button onClick={() => setIssued(null)}>Done</button>
        </div>
      )}
      {error && <div className="error">{error}</div>}
      <table className="card table">
        <thead>
          <tr>
            <th>Member</th>
            <th>Role</th>
            <th>Active keys</th>
            <th>Joined</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {members.map((m) => (
            <tr key={m.id} className={m.active ? "" : "dim"}>
              <td>
                {m.name} <div className="muted small">{m.email}</div>
              </td>
              <td>
                <select value={m.role} disabled={m.id === me.id} onChange={(e) => act(() => api(`/members/${m.id}`, { method: "PATCH", body: { role: e.target.value } }))}>
                  <option value="member">member</option>
                  <option value="admin">admin</option>
                </select>
              </td>
              <td>{m.active_keys}</td>
              <td>{fmtTime(m.created_at)}</td>
              <td className="row gap">
                {m.active && <button onClick={() => issue(m)}>Issue key</button>}
                {m.active_keys > 0 && (
                  <button className="danger" onClick={() => revokeAll(m)}>
                    Revoke keys
                  </button>
                )}
                {m.id !== me.id && (
                  <button onClick={() => act(() => api(`/members/${m.id}`, { method: "PATCH", body: { active: !m.active } }))}>{m.active ? "Deactivate" : "Reactivate"}</button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {invites.length > 0 && (
        <>
          <h2>Pending invites</h2>
          <table className="card table">
            <tbody>
              {invites.map((i) => (
                <tr key={i.id}>
                  <td>{i.email}</td>
                  <td>{i.role}</td>
                  <td>{i.expired ? "expired" : `expires ${fmtTime(i.expires_at)}`}</td>
                  <td>
                    <button onClick={() => act(() => api(`/invites/${i.id}`, { method: "DELETE" }))}>Cancel</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </>
  );
}
