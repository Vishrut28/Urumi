import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api, ApiError } from "../api";

type Info = { client_name: string; client_id: string; client_kind: string; redirect_host: string; scope: string; resource: string; user: { email: string; role: string } };

// OAuth consent: an MCP client (Claude Code, Cursor, Inspector…) asks to act as the signed-in user.
export default function Consent() {
  const [params] = useSearchParams();
  const req = params.get("req") ?? "";
  const navigate = useNavigate();
  const [info, setInfo] = useState<Info | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api<Info>(`/oauth/request?req=${encodeURIComponent(req)}`).then(setInfo, (e: ApiError) => {
      if (e.status === 401) navigate(`/login?next=${encodeURIComponent(`/consent?req=${req}`)}`, { replace: true });
      else setError(e.message);
    });
  }, [req, navigate]);

  const decide = async (approve: boolean) => {
    setBusy(true);
    try {
      const { redirect } = await api<{ redirect: string }>("/oauth/decision", { body: { req, approve } });
      window.location.assign(redirect);
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  };

  return (
    <div className="center">
      <div className="card auth">
        <h1>Connect an app</h1>
        {error && <div className="error">{error}</div>}
        {info && (
          <>
            <p>
              <b>{info.client_name}</b> wants to use the team MCP gateway as <b>{info.user.email}</b> ({info.user.role}).
            </p>
            <ul className="small">
              <li>It will see and call the tools you can use, with your permissions.</li>
              <li>Every call is in the audit log under your name.</li>
              <li>It never receives the credentials of the servers behind the gateway.</li>
              <li>You can disconnect it any time on the API keys page.</li>
            </ul>
            <p className="muted small">
              After you approve, your browser returns to <code>{info.redirect_host}</code>. Client: <code>{info.client_id.length > 60 ? info.client_id.slice(0, 60) + "…" : info.client_id}</code>
              {info.client_kind === "cimd" ? " (verified metadata document)" : " (dynamically registered)"}
            </p>
            <div className="row gap">
              <button className="primary" disabled={busy} onClick={() => decide(true)}>
                Approve
              </button>
              <button disabled={busy} onClick={() => decide(false)}>
                Deny
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
