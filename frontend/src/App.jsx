import { useCallback, useEffect, useState } from "react";
import { api, getToken, getUser, setSession } from "./api";
import Activity from "./components/Activity";
import Chat from "./components/Chat";
import Dashboard from "./components/Dashboard";
import DashboardList from "./components/DashboardList";
import Login from "./components/Login";
import Sidebar from "./components/Sidebar";

// Complete an Entra SSO redirect: /?sso_token=…&sso_user=… → store session.
(function acceptSso() {
  const q = new URLSearchParams(window.location.search);
  const token = q.get("sso_token");
  const userB64 = q.get("sso_user");
  if (token && userB64) {
    try {
      const user = JSON.parse(atob(userB64.replace(/-/g, "+").replace(/_/g, "/")));
      setSession(token, user);
    } catch {}
    window.history.replaceState({}, "", window.location.pathname);
  }
})();

export default function App() {
  const [user, setUser] = useState(getToken() ? getUser() : null);
  const [conversations, setConversations] = useState([]);
  const [activeId, setActiveId] = useState(null);
  const [showActivity, setShowActivity] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [dashboardId, setDashboardId] = useState(null);

  const refresh = useCallback(() => {
    if (!getToken()) return;
    api("/conversations").then(setConversations).catch(() => {});
  }, []);

  useEffect(refresh, [refresh, user]);

  if (!user) return <Login onLogin={setUser} />;

  return (
    <div className="layout">
      {!sidebarOpen && (
        <div className="rail">
          <button className="rail-btn" onClick={() => setSidebarOpen(true)} title="Open sidebar">☰</button>
          <button className="rail-btn" onClick={() => setActiveId(null)} title="New chat">+</button>
        </div>
      )}
      {sidebarOpen && (
      <Sidebar
        conversations={conversations}
        activeId={activeId}
        onSelect={setActiveId}
        onNew={() => setActiveId(null)}
        onCollapse={() => setSidebarOpen(false)}
        onDelete={async (id) => {
          await api(`/conversations/${id}`, { method: "DELETE" });
          if (id === activeId) setActiveId(null);
          refresh();
        }}
        onActivity={() => setShowActivity(true)}
        onDashboards={() => setDashboardId("*")}
      />
      )}
      {showActivity && <Activity onClose={() => setShowActivity(false)} />}
      {dashboardId === "*" ? (
        <DashboardList onOpen={setDashboardId} onClose={() => setDashboardId(null)} />
      ) : dashboardId ? (
        <Dashboard
          dashboardId={dashboardId}
          user={user}
          onClose={() => setDashboardId("*")}
        />
      ) : (
        <Chat
          conversationId={activeId}
          onConversationCreated={(id) => {
            setActiveId(id);
            refresh();
          }}
          onOpenDashboard={(id) => setDashboardId(id || "*")}
        />
      )}
    </div>
  );
}
