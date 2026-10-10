import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";

export default function Connect() {
  const [url, setUrl] = useState("");
  const [tools, setTools] = useState<{ name: string; server: string }[]>([]);
  useEffect(() => {
    api<{ mcp_url: string }>("/connect").then((c) => setUrl(c.mcp_url));
    api<{ name: string; server: string }[]>("/tools").then(setTools);
  }, []);
  const key = "<your API key>";
  const claude = `claude mcp add --transport http team-gateway ${url} --header "Authorization: Bearer ${key}"`;
  const cursor = JSON.stringify({ mcpServers: { "team-gateway": { url, headers: { Authorization: `Bearer ${key}` } } } }, null, 2);
  return (
    <>
      <header className="page-head">
        <div>
          <h1>Connect an MCP client</h1>
          <p className="muted">
            One URL and your own key. You never configure, or see credentials for, the servers behind it. Create a key on the <Link to="/keys">API keys</Link> page.
          </p>
        </div>
      </header>
      <section className="card">
        <h2>Gateway URL</h2>
        <pre className="copy">{url}</pre>
        <p className="muted small">Streamable HTTP. Send your key as <code>Authorization: Bearer mgw_…</code>.</p>
      </section>
      <section className="card">
        <h2>Claude Code</h2>
        <pre className="copy">{claude}</pre>
        <p className="muted small">Or sign in with your browser instead of a key (OAuth): add the server without a header, then run /mcp in Claude Code and choose Authenticate.</p>
        <pre className="copy">{`claude mcp add --transport http team-gateway ${url}`}</pre>
      </section>
      <section className="card">
        <h2>Cursor (~/.cursor/mcp.json)</h2>
        <pre className="copy">{cursor}</pre>
      </section>
      <section className="card">
        <h2>MCP Inspector</h2>
        <pre className="copy">npx @modelcontextprotocol/inspector</pre>
        <p className="muted small">Transport: Streamable HTTP · URL: {url} · Authentication: Bearer token = your key.</p>
      </section>
      <section className="card">
        <h2>Tools you will see ({tools.length})</h2>
        <div className="chips">
          {tools.map((t) => (
            <code key={t.name}>{t.name}</code>
          ))}
        </div>
      </section>
    </>
  );
}
