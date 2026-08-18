import { useCallback, useEffect, useRef, useState } from "react";
import { api, getToken, getUser, setSession } from "./api";
import Activity from "./components/Activity";
import Agents from "./components/Agents";
import Autopilot from "./components/Autopilot";
import Chat from "./components/Chat";
import Dashboard from "./components/Dashboard";
import DashboardList from "./components/DashboardList";
import Flow from "./components/Flow";
import Login from "./components/Login";
import Governance from "./components/Governance";
import Semantic from "./components/Semantic";
import Jobs from "./components/Jobs";
import Kag from "./components/Kag";
import PyBuild from "./components/PyBuild";
import Pipelines from "./components/Pipelines";
import QueryLibrary from "./components/QueryLibrary";
import Sessions from "./components/Sessions";
import Skills from "./components/Skills";
import ToolBuilder from "./components/ToolBuilder";
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
  const [convsLoaded, setConvsLoaded] = useState(false);
  const [activeId, setActiveIdRaw] = useState(null);

  // The open conversation survives a browser refresh: persist it per user and
  // restore on load — otherwise a refresh lands on an empty "New chat" and the
  // (server-saved) history looks lost.
  const setActiveId = useCallback((id) => {
    setActiveIdRaw(id);
    const key = "studio_conv:" + (getUser()?.id || "");
    if (id) localStorage.setItem(key, id);
    else localStorage.removeItem(key);
  }, []);
  const restoredRef = useRef(null);
  useEffect(() => {
    if (!user) return;
    const saved = localStorage.getItem("studio_conv:" + (user.id || ""));
    if (saved) {
      restoredRef.current = saved;   // only THIS id may be dropped as stale
      setActiveIdRaw(saved);
    }
  }, [user]);
  // Validate ONLY the id restored from localStorage, and only once the list has
  // loaded — it may point at a deleted conversation or another account's.
  // This must never run against a freshly created conversation: `conversations`
  // lags the create by one refresh, so a general "is activeId in the list?"
  // check deselects the chat the moment you ask your first question in it.
  useEffect(() => {
    if (!convsLoaded || !restoredRef.current) return;
    const stale = !conversations.some((c) => c.id === restoredRef.current);
    if (stale && activeId === restoredRef.current) setActiveId(null);
    restoredRef.current = null;      // one-shot
  }, [convsLoaded, conversations, activeId, setActiveId]);
  const [showActivity, setShowActivity] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [dashboardId, setDashboardId] = useState(null);
  const [showQueries, setShowQueries] = useState(false);
  const [showPipelines, setShowPipelines] = useState(false);
  const [showGovernance, setShowGovernance] = useState(false);
  const [showSemantic, setShowSemantic] = useState(false);
  const [showJobs, setShowJobs] = useState(false);
  const [showPy, setShowPy] = useState(false);
  const [showSessions, setShowSessions] = useState(false);
  const [showAgents, setShowAgents] = useState(false);
  const [showFlow, setShowFlow] = useState(false);
  const [showSkills, setShowSkills] = useState(false);
  const [showAutopilot, setShowAutopilot] = useState(false);
  const [showToolBuilder, setShowToolBuilder] = useState(false);
  const [showKag, setShowKag] = useState(false);
  // Bumped when a user connects/removes a key so the model menu refetches.
  const [modelsEpoch, setModelsEpoch] = useState(0);

  const refresh = useCallback(() => {
    if (!getToken()) return;
    api("/conversations")
      .then((c) => {
        setConversations(c);
        setConvsLoaded(true);
      })
      .catch(() => {});
  }, []);

  useEffect(refresh, [refresh, user]);

  // Poll conversations so a background task finishing in another chat lights up
  // its blue dot without a manual refresh.
  useEffect(() => {
    if (!user) return;
    const h = setInterval(refresh, 6000);
    return () => clearInterval(h);
  }, [user, refresh]);

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
        onQueries={() => setShowQueries(true)}
        onPipelines={() => setShowPipelines(true)}
        onGovernance={() => setShowGovernance(true)}
        onSemantic={() => setShowSemantic(true)}
        onJobs={() => setShowJobs(true)}
        onPyBuild={() => setShowPy(true)}
        onSessions={() => setShowSessions(true)}
        onAgents={() => setShowAgents(true)}
        onFlow={() => setShowFlow(true)}
        onSkills={() => setShowSkills(true)}
        onAutopilot={() => setShowAutopilot(true)}
        onToolBuilder={() => setShowToolBuilder(true)}
        onKag={() => setShowKag(true)}
        onKeysChanged={() => setModelsEpoch((n) => n + 1)}
        onRename={async (id, title) => {
          await api(`/conversations/${id}`, {
            method: "PATCH",
            body: JSON.stringify({ title }),
          });
          refresh();
        }}
      />
      )}
      {showActivity && <Activity onClose={() => setShowActivity(false)} />}
      {showSkills ? (
        <Skills onClose={() => setShowSkills(false)} />
      ) : showAutopilot ? (
        <Autopilot
          user={user}
          onClose={() => setShowAutopilot(false)}
          onOpenJobs={() => { setShowAutopilot(false); setShowJobs(true); }}
        />
      ) : showKag ? (
        <Kag user={user} onClose={() => setShowKag(false)} />
      ) : showToolBuilder ? (
        <ToolBuilder
          onClose={() => setShowToolBuilder(false)}
          onOpenJobs={() => { setShowToolBuilder(false); setShowJobs(true); }}
        />
      ) : showFlow ? (
        <Flow onClose={() => setShowFlow(false)} onOpenJobs={() => { setShowFlow(false); setShowJobs(true); }} />
      ) : showAgents ? (
        <Agents onClose={() => setShowAgents(false)} />
      ) : showSessions ? (
        <Sessions
          onClose={() => setShowSessions(false)}
          onResume={(state) => {
            // Resuming drops back into the exact conversation; its next turn
            // replays the same stable prefix, reusing the provider cache.
            setShowSessions(false);
            if (state?.conversation_id) setActiveId(state.conversation_id);
          }}
        />
      ) : showPy ? (
        <PyBuild onClose={() => setShowPy(false)} />
      ) : showJobs ? (
        <Jobs onClose={() => setShowJobs(false)} />
      ) : showGovernance ? (
        <Governance onClose={() => setShowGovernance(false)} />
      ) : showSemantic ? (
        <Semantic user={user} onClose={() => setShowSemantic(false)} />
      ) : showPipelines ? (
        <Pipelines onClose={() => setShowPipelines(false)} />
      ) : showQueries ? (
        <QueryLibrary onClose={() => setShowQueries(false)} />
      ) : dashboardId === "*" ? (
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
          modelsEpoch={modelsEpoch}
        />
      )}
    </div>
  );
}
