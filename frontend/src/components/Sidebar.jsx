import { useEffect, useRef, useState } from "react";
import { clearSession, getUser } from "../api";
import KeySettings from "./KeySettings";
import ShareDialog from "./ShareDialog";

export default function Sidebar({
  conversations, activeId, onSelect, onNew, onDelete, onActivity, onCollapse,
  onDashboards, onRename, onKeysChanged, onQueries, onPipelines, onGovernance, onJobs, onPyBuild,
  onSessions, onAgents,
}) {
  const user = getUser();
  const [menu, setMenu] = useState(null);     // {id, title, x, y, canEdit, owned}
  const [renaming, setRenaming] = useState(null); // conversation id
  const [draft, setDraft] = useState("");
  const [sharing, setSharing] = useState(null);   // {id, title}
  const [keysOpen, setKeysOpen] = useState(false);
  const inputRef = useRef(null);

  // The menu is positioned in viewport coordinates, so any scroll, resize or
  // outside click must dismiss it rather than leave it stranded.
  useEffect(() => {
    if (!menu) return;
    const close = () => setMenu(null);
    const onKey = (e) => e.key === "Escape" && close();
    window.addEventListener("click", close);
    window.addEventListener("resize", close);
    window.addEventListener("scroll", close, true);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("click", close);
      window.removeEventListener("resize", close);
      window.removeEventListener("scroll", close, true);
      window.removeEventListener("keydown", onKey);
    };
  }, [menu]);

  useEffect(() => {
    if (renaming) inputRef.current?.select();
  }, [renaming]);

  function openMenu(e, c) {
    e.preventDefault();
    e.stopPropagation();
    setMenu({
      id: c.id,
      title: c.title,
      canEdit: c.can_edit !== false,
      owned: !c.shared,
      // Keep the menu inside the viewport when the click is near an edge.
      x: Math.min(e.clientX, window.innerWidth - 180),
      y: Math.min(e.clientY, window.innerHeight - 150),
    });
  }

  function startRename(c) {
    setRenaming(c.id);
    setDraft(c.title);
    setMenu(null);
  }

  function commitRename(id) {
    const title = draft.trim();
    setRenaming(null);
    const current = conversations.find((c) => c.id === id);
    if (title && current && title !== current.title) onRename?.(id, title);
  }

  return (
    <aside className="sidebar">
      <div className="sidebar-top">
        <button className="new-chat" onClick={onNew}>
          + New chat
        </button>
        <button className="collapse-btn" onClick={onCollapse} title="Close sidebar">
          «
        </button>
      </div>
      <div className="conv-list">
        {conversations.map((c) => (
          <div
            key={c.id}
            className={"conv" + (c.id === activeId ? " conv-active" : "")}
            onClick={() => renaming !== c.id && onSelect(c.id)}
            onContextMenu={(e) => openMenu(e, c)}
            title={c.shared ? `Shared by ${c.owner_email || "someone"}` : "Right-click for options"}
          >
            {renaming === c.id ? (
              <input
                ref={inputRef}
                className="conv-rename"
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                onBlur={() => commitRename(c.id)}
                onClick={(e) => e.stopPropagation()}
                onKeyDown={(e) => {
                  if (e.key === "Enter") commitRename(c.id);
                  if (e.key === "Escape") setRenaming(null);
                }}
                autoFocus
              />
            ) : (
              <>
                <span className="conv-title">
                  {c.shared && <span className="conv-shared" title="Shared with you">◈ </span>}
                  {c.title}
                </span>
                <button
                  className="conv-del"
                  title={c.shared ? "Only the owner can delete this" : "Delete"}
                  disabled={c.shared}
                  onClick={(e) => {
                    e.stopPropagation();
                    onDelete(c.id);
                  }}
                >
                  ×
                </button>
              </>
            )}
          </div>
        ))}
        {conversations.length === 0 && <div className="conv-empty">No conversations yet</div>}
      </div>

      {menu && (
        <div
          className="ctx-menu"
          style={{ left: menu.x, top: menu.y }}
          onClick={(e) => e.stopPropagation()}
        >
          <button
            className="ctx-item"
            disabled={!menu.canEdit}
            title={menu.canEdit ? "" : "You have view-only access"}
            onClick={() => startRename(conversations.find((c) => c.id === menu.id) || menu)}
          >
            ✎ Rename
          </button>
          <button
            className="ctx-item"
            onClick={() => {
              setSharing({ id: menu.id, title: menu.title });
              setMenu(null);
            }}
          >
            ◈ Share…
          </button>
          <button
            className="ctx-item ctx-danger"
            disabled={!menu.owned}
            title={menu.owned ? "" : "Only the owner can delete this"}
            onClick={() => {
              onDelete(menu.id);
              setMenu(null);
            }}
          >
            ✕ Delete
          </button>
        </div>
      )}

      {keysOpen && <KeySettings onClose={() => setKeysOpen(false)} onChanged={onKeysChanged} />}

      {sharing && (
        <ShareDialog
          conversationId={sharing.id}
          title={sharing.title}
          onClose={() => setSharing(null)}
        />
      )}

      <div className="sidebar-footer">
        <button className="logout" onClick={onDashboards} style={{ marginBottom: 8 }}>
          ▦ Dashboards
        </button>
        <button className="logout" onClick={onQueries} style={{ marginBottom: 8 }}>
          ⌗ Saved SQL
        </button>
        <button className="logout" onClick={onPipelines} style={{ marginBottom: 8 }}>
          ⑃ Pipelines
        </button>
        <button className="logout" onClick={onJobs} style={{ marginBottom: 8 }}>
          ⚙ Jobs
        </button>
        <button className="logout" onClick={onPyBuild} style={{ marginBottom: 8 }}>
          ⟨⟩ Build Python
        </button>
        <button className="logout" onClick={onSessions} style={{ marginBottom: 8 }}>
          ⟳ Sessions
        </button>
        <button className="logout" onClick={onAgents} style={{ marginBottom: 8 }}>
          ⚇ Agents
        </button>
        {user?.role === "admin" && (
          <button className="logout" onClick={onGovernance} style={{ marginBottom: 8 }}>
            ⚖ Governance
          </button>
        )}
        <button className="logout" onClick={() => setKeysOpen(true)} style={{ marginBottom: 8 }}>
          ⚿ API keys
        </button>
        <button className="logout" onClick={onActivity} style={{ marginBottom: 8 }}>
          ⏱ Activity{user?.role === "admin" ? " (all users)" : ""}
        </button>
        <div className="userline">
          <span className="avatar">{(user?.name || "?")[0].toUpperCase()}</span>
          <div>
            <div className="username">{user?.name}</div>
            <div className={"rolebadge role-" + user?.role}>{user?.role}</div>
          </div>
        </div>
        <button
          className="logout"
          onClick={() => {
            clearSession();
            window.location.reload();
          }}
        >
          Sign out
        </button>
      </div>
    </aside>
  );
}
