import { useCallback, useEffect, useRef, useState } from "react";
import { Navigate, Route, Routes, useLocation, useMatch, useNavigate, useParams } from "react-router-dom";
import { api, exchangeSsoCode, getToken, getUser, setSession } from "./api";
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

// Complete an Entra SSO redirect: /?sso_code=… → POST it for the session.
// The redirect never carries the token itself (a URL lands in history, Referer
// headers and every proxy log on the path); the backend parks it under this
// single-use 60s code and /auth/sso/exchange hands it over exactly once.
//
// Runs at module load, before the router mounts, so the router only ever sees
// the scrubbed URL. The exchange resolves after React has already read
// localStorage, so a successful handoff reloads onto the stored session
// instead of leaving the login screen up in front of it.
(function acceptSso() {
  const q = new URLSearchParams(window.location.search);
  const code = q.get("sso_code");
  if (!code) return;
  // Scrub first: the code is spent by the POST below, so a refresh (or the
  // router reading the URL) must never see it again.
  window.history.replaceState({}, "", window.location.pathname);
  exchangeSsoCode(code)
    .then(({ access_token, user }) => {
      setSession(access_token, user);
      window.location.reload();
    })
    .catch(() => {});   // spent/expired code → the plain login screen
})();

// Every surface is a route: the URL is the only "which view is open" state,
// so the sidebar just navigates and deep links / back-button work for free.
// Activity is the one exception — it is a modal over whatever is open, not a
// page, so it stays a boolean.

// "/" and "/c/:conversationId" both render this. React Router wraps a route's
// element in an unkeyed RenderedRoute, so moving between the two keeps the
// same Chat instance mounted — which matters because onConversationCreated
// fires mid-stream on the first answer; a remount would drop it.
function ChatPage({ modelsEpoch, onConversationCreated }) {
  const { conversationId = null } = useParams();
  const navigate = useNavigate();
  return (
    <Chat
      conversationId={conversationId}
      onConversationCreated={onConversationCreated}
      onOpenDashboard={(id) => navigate(id ? `/dashboards/${id}` : "/dashboards")}
      modelsEpoch={modelsEpoch}
    />
  );
}

function DashboardPage({ user }) {
  const { dashboardId } = useParams();
  const navigate = useNavigate();
  return <Dashboard dashboardId={dashboardId} user={user} onClose={() => navigate("/dashboards")} />;
}

export default function App() {
  const navigate = useNavigate();
  const location = useLocation();
  const [user, setUser] = useState(getToken() ? getUser() : null);
  const [conversations, setConversations] = useState([]);
  const [convsLoaded, setConvsLoaded] = useState(false);
  // The active conversation IS the URL; nothing else may hold it.
  const activeId = useMatch("/c/:conversationId")?.params.conversationId ?? null;
  const onChatRoot = location.pathname === "/";
  const convKey = "studio_conv:" + (user?.id || "");

  // The open conversation survives a browser refresh: persist it per user and
  // restore on load — otherwise a refresh lands on an empty "New chat" and the
  // (server-saved) history looks lost. Only a landing on "/" is redirected,
  // exactly once; a deep link to /jobs or /c/<other> is left alone.
  // Phases: "init" → ("validating" saved id | "done") → "done".
  const restore = useRef({ phase: "init", id: null });
  useEffect(() => {
    if (!user || restore.current.phase !== "init") return;
    const saved = localStorage.getItem(convKey);
    if (saved && onChatRoot) {
      restore.current = { phase: "validating", id: saved };
      navigate(`/c/${saved}`, { replace: true });
    } else {
      restore.current = { phase: "done", id: null };
    }
  }, [user]);   // deliberately not re-run on route changes: one-shot
  // Validate ONLY the id restored from localStorage, and only once the list has
  // loaded — it may point at a deleted conversation or another account's.
  // This must never run against a freshly created conversation: `conversations`
  // lags the create by one refresh, so a general "is activeId in the list?"
  // check deselects the chat the moment you ask your first question in it.
  useEffect(() => {
    if (!convsLoaded || restore.current.phase !== "validating") return;
    const { id } = restore.current;
    restore.current = { phase: "done", id: null };   // one-shot
    if (!conversations.some((c) => c.id === id)) {
      localStorage.removeItem(convKey);
      if (activeId === id) navigate("/", { replace: true });
    }
  }, [convsLoaded, conversations, activeId, convKey, navigate]);
  // Persist: an open chat is remembered; "New chat" (landing on "/") forgets
  // it; visiting another surface leaves the memory untouched. Skipped until the
  // restore has settled so the initial "/" render can't wipe the saved id
  // before the redirect to it lands.
  useEffect(() => {
    if (!user || restore.current.phase !== "done") return;
    if (activeId) localStorage.setItem(convKey, activeId);
    else if (onChatRoot) localStorage.removeItem(convKey);
  }, [user, activeId, onChatRoot, convKey]);

  const [showActivity, setShowActivity] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  // Bumped when a user connects/removes a key so the model menu refetches.
  const [modelsEpoch, setModelsEpoch] = useState(0);

  // Epoch guard: the 6s poll and a post-action refresh can be in flight at
  // once, and the slower (staler) response must not clobber the newer one —
  // without this a just-moved chat visibly jumps back to its old folder.
  const refreshEpochRef = useRef(0);
  const refresh = useCallback(() => {
    if (!getToken()) return;
    const epoch = ++refreshEpochRef.current;
    api("/conversations")
      .then((c) => {
        if (epoch !== refreshEpochRef.current) return; // superseded — drop it
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

  const go = (path) => () => navigate(path);
  const home = go("/");
  const chat = (
    <ChatPage
      modelsEpoch={modelsEpoch}
      onConversationCreated={(id) => { navigate(`/c/${id}`); refresh(); }}
    />
  );

  return (
    <div className="layout">
      {!sidebarOpen && (
        <div className="rail">
          <button className="rail-btn" onClick={() => setSidebarOpen(true)} title="Open sidebar">☰</button>
          <button className="rail-btn" onClick={home} title="New chat">+</button>
        </div>
      )}
      {sidebarOpen && (
      <Sidebar
        conversations={conversations}
        activeId={activeId}
        onSelect={(id) => navigate(`/c/${id}`)}
        onNew={home}
        onRefresh={refresh}
        onCollapse={() => setSidebarOpen(false)}
        onDelete={async (id) => {
          await api(`/conversations/${id}`, { method: "DELETE" });
          if (id === activeId) navigate("/");
          refresh();
        }}
        onActivity={() => setShowActivity(true)}
        onDashboards={go("/dashboards")}
        onQueries={go("/queries")}
        onPipelines={go("/pipelines")}
        onGovernance={go("/governance")}
        onSemantic={go("/semantic")}
        onJobs={go("/jobs")}
        onPyBuild={go("/py")}
        onSessions={go("/sessions")}
        onAgents={go("/agents")}
        onFlow={go("/flow")}
        onSkills={go("/skills")}
        onAutopilot={go("/autopilot")}
        onToolBuilder={go("/toolbuilder")}
        onKag={go("/kag")}
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
      <Routes>
        <Route path="/skills" element={<Skills onClose={home} />} />
        <Route path="/autopilot" element={<Autopilot user={user} onClose={home} onOpenJobs={go("/jobs")} />} />
        <Route path="/kag" element={<Kag user={user} onClose={home} />} />
        <Route path="/toolbuilder" element={<ToolBuilder onClose={home} onOpenJobs={go("/jobs")} />} />
        <Route path="/flow" element={<Flow onClose={home} onOpenJobs={go("/jobs")} />} />
        <Route path="/agents" element={<Agents onClose={home} />} />
        <Route
          path="/sessions"
          element={
            <Sessions
              onClose={home}
              onResume={(state) => {
                // Resuming drops back into the exact conversation; its next turn
                // replays the same stable prefix, reusing the provider cache.
                navigate(state?.conversation_id ? `/c/${state.conversation_id}` : "/");
              }}
            />
          }
        />
        <Route path="/py" element={<PyBuild onClose={home} />} />
        <Route path="/jobs" element={<Jobs onClose={home} />} />
        <Route path="/governance" element={<Governance onClose={home} />} />
        <Route path="/semantic" element={<Semantic user={user} onClose={home} />} />
        <Route path="/pipelines" element={<Pipelines onClose={home} />} />
        <Route path="/queries" element={<QueryLibrary onClose={home} />} />
        <Route
          path="/dashboards"
          element={<DashboardList onOpen={(id) => navigate(`/dashboards/${id}`)} onClose={home} />}
        />
        <Route path="/dashboards/:dashboardId" element={<DashboardPage user={user} />} />
        <Route path="/c/:conversationId" element={chat} />
        <Route path="/" element={chat} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </div>
  );
}
