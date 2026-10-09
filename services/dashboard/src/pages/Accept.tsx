import { FormEvent, useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api, Me } from "../api";

export default function Accept({ onLogin }: { onLogin: (m: Me) => void }) {
  const [params] = useSearchParams();
  const token = params.get("token") ?? "";
  const navigate = useNavigate();
  const [invite, setInvite] = useState<{ email: string; role: string } | null>(null);
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    api<{ email: string; role: string }>(`/invites/lookup?token=${encodeURIComponent(token)}`).then(setInvite, (e) => setError(e.message));
  }, [token]);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    try {
      onLogin(await api<Me>("/invites/accept", { body: { token, name, password } }));
      navigate("/keys");
    } catch (err) {
      setError((err as Error).message);
    }
  };

  return (
    <div className="center">
      <form className="card auth" onSubmit={submit}>
        <h1>Join the team</h1>
        {invite ? (
          <p className="muted">
            You were invited as <b>{invite.email}</b> ({invite.role}).
          </p>
        ) : (
          !error && <p className="muted">Checking invite…</p>
        )}
        {invite && (
          <>
            <label>
              Your name
              <input value={name} onChange={(e) => setName(e.target.value)} required autoFocus />
            </label>
            <label>
              Password (10+ characters)
              <input type="password" minLength={10} value={password} onChange={(e) => setPassword(e.target.value)} required />
            </label>
            <button className="primary">Create account</button>
          </>
        )}
        {error && <div className="error">{error}</div>}
      </form>
    </div>
  );
}
