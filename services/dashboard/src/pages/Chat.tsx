import { FormEvent, useEffect, useRef, useState } from "react";
import { api } from "../api";
import { useSession } from "../main";

type Step = { tool: string; arguments: unknown; ok: boolean; duration_ms: number; result_preview: string };
type Turn = { role: "user" | "assistant"; content: string; steps?: Step[]; error?: boolean };

const EXAMPLES = [
  "What orders came in today?",
  "Create a product called 'Gateway Mug' priced at 14.50",
  "What is the total of today's orders in EUR?",
  "Get shipping quotes for a 2 kg parcel to Germany",
];

export default function Chat() {
  const { me } = useSession();
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [info, setInfo] = useState<{ llm_model: string; chat_enabled: boolean } | null>(null);
  const end = useRef<HTMLDivElement>(null);

  useEffect(() => {
    api<{ llm_model: string; chat_enabled: boolean }>("/connect").then(setInfo);
  }, []);
  useEffect(() => end.current?.scrollIntoView({ behavior: "smooth" }), [turns, busy]);

  const send = async (text: string) => {
    if (!text.trim() || busy) return;
    const history = [...turns.filter((t) => !t.error), { role: "user" as const, content: text }];
    setTurns([...turns, { role: "user", content: text }]);
    setInput("");
    setBusy(true);
    try {
      const res = await api<{ reply: string; steps: Step[] }>("/chat", { body: { messages: history.map(({ role, content }) => ({ role, content })) } });
      setTurns((t) => [...t, { role: "assistant", content: res.reply, steps: res.steps }]);
    } catch (e) {
      setTurns((t) => [...t, { role: "assistant", content: (e as Error).message, error: true }]);
    } finally {
      setBusy(false);
    }
  };

  const submit = (e: FormEvent) => {
    e.preventDefault();
    send(input);
  };

  return (
    <div className="chat">
      <header className="page-head">
        <div>
          <h1>Chat</h1>
          <p className="muted">
            Answers come from tool calls through the gateway, made as <b>{me.email}</b>. Every call shows up in the audit log.
            {info && ` Model: ${info.llm_model}.`}
          </p>
        </div>
        {turns.length > 0 && <button onClick={() => setTurns([])}>New chat</button>}
      </header>
      {info && !info.chat_enabled && <div className="error">The chat agent has no LLM API key configured.</div>}
      <div className="messages">
        {turns.length === 0 && (
          <div className="examples">
            {EXAMPLES.map((x) => (
              <button key={x} onClick={() => send(x)}>
                {x}
              </button>
            ))}
          </div>
        )}
        {turns.map((t, i) => (
          <div key={i} className={`msg ${t.role} ${t.error ? "error" : ""}`}>
            {t.steps && t.steps.length > 0 && (
              <div className="steps">
                {t.steps.map((s, j) => (
                  <details key={j} className={s.ok ? "ok" : "fail"}>
                    <summary>
                      {s.ok ? "✓" : "✗"} <code>{s.tool}</code> <span className="muted small">{s.duration_ms} ms</span>
                    </summary>
                    <div className="small">
                      <div className="muted">arguments</div>
                      <pre>{JSON.stringify(s.arguments, null, 2)}</pre>
                      <div className="muted">result</div>
                      <pre>{s.result_preview}</pre>
                    </div>
                  </details>
                ))}
              </div>
            )}
            <div className="text">{t.content}</div>
          </div>
        ))}
        {busy && <div className="msg assistant muted">Thinking and calling tools…</div>}
        <div ref={end} />
      </div>
      <form className="composer" onSubmit={submit}>
        <input value={input} onChange={(e) => setInput(e.target.value)} placeholder="Ask about products, orders, shipping…" disabled={busy} autoFocus />
        <button className="primary" disabled={busy || !input.trim()}>
          Send
        </button>
      </form>
    </div>
  );
}
