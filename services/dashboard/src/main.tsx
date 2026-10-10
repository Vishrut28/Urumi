import React, { createContext, useContext, useEffect, useState } from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, Navigate, NavLink, Route, Routes, useNavigate } from "react-router-dom";
import { api, Me } from "./api";
import "./style.css";
import Accept from "./pages/Accept";
import Audit from "./pages/Audit";
import Chat from "./pages/Chat";
import Consent from "./pages/Consent";
import Connect from "./pages/Connect";
import Keys from "./pages/Keys";
import Login from "./pages/Login";
import Members from "./pages/Members";
import Servers from "./pages/Servers";
import Stores from "./pages/Stores";

type Session = { me: Me; setMe: (m: Me | null) => void; features: { stores: boolean } };
const SessionContext = createContext<Session | null>(null);
export const useSession = () => useContext(SessionContext)!;

function Shell({ me, setMe }: { me: Me; setMe: (m: Me | null) => void }) {
  const navigate = useNavigate();
  const [stores, setStores] = useState(false);
  useEffect(() => {
    api("/stores").then(() => setStores(true), () => setStores(false));
  }, []);
  const logout = async () => {
    await api("/auth/logout", { method: "POST" });
    setMe(null);
    navigate("/login");
  };
  const admin = me.role === "admin";
  return (
    <SessionContext.Provider value={{ me, setMe, features: { stores } }}>
      <div className="layout">
        <aside className="nav">
          <div className="brand">MCP Gateway</div>
          <NavLink to="/servers">Servers</NavLink>
          {stores && <NavLink to="/stores">Stores</NavLink>}
          <NavLink to="/chat">Chat</NavLink>
          <NavLink to="/connect">Connect a client</NavLink>
          <NavLink to="/keys">API keys</NavLink>
          {admin && <NavLink to="/members">Members</NavLink>}
          <NavLink to="/audit">Audit log</NavLink>
          <div className="spacer" />
          <div className="who">
            <div>{me.name || me.email}</div>
            <div className="muted small">
              {me.email} · <span className={`role role-${me.role}`}>{me.role}</span>
            </div>
            <button className="link" onClick={logout}>
              Sign out
            </button>
          </div>
        </aside>
        <main className="content">
          <Routes>
            <Route path="/servers" element={<Servers />} />
            <Route path="/stores" element={<Stores />} />
            <Route path="/chat" element={<Chat />} />
            <Route path="/connect" element={<Connect />} />
            <Route path="/keys" element={<Keys />} />
            <Route path="/members" element={admin ? <Members /> : <Navigate to="/servers" />} />
            <Route path="/audit" element={<Audit />} />
            <Route path="*" element={<Navigate to="/servers" />} />
          </Routes>
        </main>
      </div>
    </SessionContext.Provider>
  );
}

function App() {
  const [me, setMe] = useState<Me | null | undefined>(undefined);
  useEffect(() => {
    api<Me>("/me").then(setMe, () => setMe(null));
  }, []);
  if (me === undefined) return <div className="center muted">Loading…</div>;
  return (
    <Routes>
      <Route path="/login" element={me ? <Navigate to="/servers" /> : <Login onLogin={setMe} />} />
      <Route path="/accept" element={<Accept onLogin={setMe} />} />
      <Route path="/consent" element={<Consent />} />
      <Route path="*" element={me ? <Shell me={me} setMe={setMe} /> : <Navigate to="/login" />} />
    </Routes>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </React.StrictMode>,
);
