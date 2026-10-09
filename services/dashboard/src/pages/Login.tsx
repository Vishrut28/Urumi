import { FormEvent, useState } from "react";
import { api, Me } from "../api";

export default function Login({ onLogin }: { onLogin: (m: Me) => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError("");
    try {
      onLogin(await api<Me>("/auth/login", { body: { email, password } }));
    } catch (err) {
      setError((err as Error).message);
    }
  };
  return (
    <div className="center">
      <form className="card auth" onSubmit={submit}>
        <h1>MCP Gateway</h1>
        <p className="muted">Sign in to manage servers, keys and the team.</p>
        <label>
          Email
          <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoFocus required />
        </label>
        <label>
          Password
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} required />
        </label>
        {error && <div className="error">{error}</div>}
        <button className="primary">Sign in</button>
      </form>
    </div>
  );
}
