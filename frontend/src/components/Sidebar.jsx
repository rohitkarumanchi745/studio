import { useEffect, useRef, useState } from "react";
import { api, clearSession, getUser } from "../api";
import KeySettings from "./KeySettings";
import DataConnections from "./DataConnections";
import ShareDialog from "./ShareDialog";

export default function Sidebar({
  conversations, activeId, onSelect, onNew, onDelete, onActivity, onCollapse,
  onDashboards, onRename, onRefresh, onKeysChanged, onQueries, onPipelines, onGovernance, onSemantic, onJobs, onPyBuild,
  onSessions, onAgents, onFlow, onSkills, onAutopilot, onToolBuilder, onKag, onRedTeam,
}) {
  const user = getUser();
  const [menu, setMenu] = useState(null);     // {id, title, x, y, canEdit, owned, folderId}
  const [renaming, setRenaming] = useState(null); // conversation id
  const [draft, setDraft] = useState("");
  const [sharing, setSharing] = useState(null);   // {id, title}
  const [keysOpen, setKeysOpen] = useState(false);
  const [dataConnOpen, setDataConnOpen] = useState(false);
  const inputRef = useRef(null);

  // Folders: personal chat organization. Collapse state is per-device.
  const [folders, setFolders] = useState([]);
  const [folderMenu, setFolderMenu] = useState(null); // {id, name, x, y}
  const [renamingFolder, setRenamingFolder] = useState(null);
  const [folderDraft, setFolderDraft] = useState("");
  const [creatingFolder, setCreatingFolder] = useState(false);
  const [collapsed, setCollapsed] = useState(() => {
    try { return JSON.parse(localStorage.getItem("studio_folders_collapsed")) || {}; }
    catch { return {}; }
  });

  // The tool buttons are a tall stack; collapsed by default they stop
  // swallowing the sidebar, so the chat history always has room to render.
  const [toolsOpen, setToolsOpen] = useState(
    () => localStorage.getItem("studio_tools_open") === "1");
  function toggleTools() {
    setToolsOpen((o) => {
      try { localStorage.setItem("studio_tools_open", o ? "0" : "1"); }
      catch { /* private mode */ }
      return !o;
    });
  }

  const loadFolders = () =>
    api("/folders").then((d) => setFolders(d.folders || [])).catch(() => {});
  // Same cadence as the conversations poll in App.jsx, so a folder created,
  // renamed or deleted in another tab/device shows up here too — otherwise its
  // chats would silently fall through to the root while the folder exists.
  useEffect(() => {
    loadFolders();
    const h = setInterval(loadFolders, 6000);
    return () => clearInterval(h);
  }, []);

  function toggleFolder(fid) {
    setCollapsed((prev) => {
      const next = { ...prev, [fid]: !prev[fid] };
      try { localStorage.setItem("studio_folders_collapsed", JSON.stringify(next)); }
      catch { /* private mode */ }
      return next;
    });
  }

  async function createFolder(name) {
    setCreatingFolder(false);
    const clean = name.trim();
    if (!clean) return;
    try { await api("/folders", { method: "POST", body: JSON.stringify({ name: clean }) }); }
    catch { /* leave the list as-is */ }
    loadFolders();
  }

  async function commitFolderRename(fid) {
    const name = folderDraft.trim();
    setRenamingFolder(null);
    const current = folders.find((f) => f.id === fid);
    if (!name || !current || name === current.name) return;
    try { await api(`/folders/${fid}`, { method: "PATCH", body: JSON.stringify({ name }) }); }
    catch { /* keep old name */ }
    loadFolders();
  }

  async function deleteFolder(fid) {
    setFolderMenu(null);
    try { await api(`/folders/${fid}`, { method: "DELETE" }); }
    catch { /* keep it */ }
    loadFolders();
    onRefresh?.();   // its chats are back at the root
  }

  async function moveTo(cid, fid) {
    setMenu(null);
    try {
      await api(`/conversations/${cid}/folder`, {
        method: "POST", body: JSON.stringify({ folder_id: fid }),
      });
    } catch { /* stays where it was */ }
    onRefresh?.();
  }

  // The menus are positioned in viewport coordinates, so any scroll, resize or
  // outside click must dismiss them rather than leave them stranded.
  useEffect(() => {
    if (!menu && !folderMenu) return;
    const close = () => { setMenu(null); setFolderMenu(null); };
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
  }, [menu, folderMenu]);

  useEffect(() => {
    if (renaming) inputRef.current?.select();
  }, [renaming]);

  function openMenu(e, c) {
    e.preventDefault();
    e.stopPropagation();
    setFolderMenu(null);
    setMenu({
      id: c.id,
      title: c.title,
      canEdit: c.can_edit !== false,
      owned: !c.shared,
      folderId: c.folder_id || null,
      // Keep the menu inside the viewport when the click is near an edge.
      x: Math.min(e.clientX, window.innerWidth - 180),
      y: Math.min(e.clientY, window.innerHeight - 260),
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

  // A folder groups the chats filed into it; anything unfiled (or whose folder
  // vanished) stays at the root, so a chat can never become unreachable.
  const folderIds = new Set(folders.map((f) => f.id));
  const inFolder = (fid) => conversations.filter((c) => c.folder_id === fid);
  const rootConvs = conversations.filter(
    (c) => !c.folder_id || !folderIds.has(c.folder_id));

  const renderConv = (c, filed) => (
    <div
      key={c.id}
      className={"conv" + (c.id === activeId ? " conv-active" : "") + (filed ? " conv-filed" : "")}
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
            if (e.key === "Enter" && !e.nativeEvent.isComposing) commitRename(c.id);
            if (e.key === "Escape") setRenaming(null);
          }}
          autoFocus
        />
      ) : (
        <>
          <span
            className="conv-title"
            onDoubleClick={() => c.can_edit !== false && startRename(c)}
          >
            {c.running > 0 ? (
              <span className="conv-run" title="A task is running here">◌ </span>
            ) : c.unseen > 0 ? (
              <span className="conv-dot" title="A task finished — open to view" />
            ) : null}
            {c.shared && <span className="conv-shared" title="Shared with you">◈ </span>}
            {c.title}
          </span>
          <button
            className="conv-edit"
            title={c.can_edit === false ? "You have view-only access" : "Rename"}
            disabled={c.can_edit === false}
            onClick={(e) => {
              e.stopPropagation();
              startRename(c);
            }}
          >
            ✎
          </button>
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
  );

  return (
    <aside className="sidebar">
      <div className="sidebar-top">
        <button className="new-chat" onClick={onNew}>
          + New chat
        </button>
        <button
          className="collapse-btn"
          onClick={() => setCreatingFolder(true)}
          title="New folder"
        >
          ⊞
        </button>
        <button className="collapse-btn" onClick={onCollapse} title="Close sidebar">
          «
        </button>
      </div>
      <div className="conv-label">Chats</div>
      <div className="conv-list">
        {creatingFolder && (
          <div className="folder-row">
            <input
              className="conv-rename"
              placeholder="Folder name"
              onBlur={(e) => createFolder(e.target.value)}
              onKeyDown={(e) => {
                // isComposing: an IME's confirm-Enter must not commit mid-typing.
                if (e.key === "Enter" && !e.nativeEvent.isComposing) createFolder(e.target.value);
                if (e.key === "Escape") setCreatingFolder(false);
              }}
              autoFocus
            />
          </div>
        )}
        {folders.map((f) => {
          const convs = inFolder(f.id);
          const running = convs.reduce((n, c) => n + (c.running || 0), 0);
          const unseen = convs.reduce((n, c) => n + (c.unseen || 0), 0);
          return (
            <div key={f.id}>
              <div
                className={"folder-row" +
                  (collapsed[f.id] && convs.some((c) => c.id === activeId)
                    ? " conv-active" : "")}
                onClick={() => renamingFolder !== f.id && toggleFolder(f.id)}
                onContextMenu={(e) => {
                  e.preventDefault();
                  e.stopPropagation();
                  setMenu(null);
                  setFolderMenu({
                    id: f.id, name: f.name,
                    x: Math.min(e.clientX, window.innerWidth - 180),
                    y: Math.min(e.clientY, window.innerHeight - 120),
                  });
                }}
                title="Right-click for options"
              >
                {renamingFolder === f.id ? (
                  <input
                    className="conv-rename"
                    value={folderDraft}
                    onChange={(e) => setFolderDraft(e.target.value)}
                    onBlur={() => commitFolderRename(f.id)}
                    onClick={(e) => e.stopPropagation()}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" && !e.nativeEvent.isComposing) commitFolderRename(f.id);
                      if (e.key === "Escape") setRenamingFolder(null);
                    }}
                    autoFocus
                  />
                ) : (
                  <>
                    <span
                      className="folder-name"
                      onDoubleClick={() => {
                        setRenamingFolder(f.id);
                        setFolderDraft(f.name);
                      }}
                    >
                      <span className="folder-caret">{collapsed[f.id] ? "▸" : "▾"}</span>
                      {" "}▣ {f.name}
                      <span className="folder-count">
                        {collapsed[f.id] && running > 0 ? " ◌" : ""}
                        {collapsed[f.id] && !running && unseen > 0 ? " ●" : ""}
                        {" "}{convs.length}
                      </span>
                    </span>
                    <button
                      className="conv-edit"
                      title="Rename folder"
                      onClick={(e) => {
                        e.stopPropagation();
                        setRenamingFolder(f.id);
                        setFolderDraft(f.name);
                      }}
                    >
                      ✎
                    </button>
                  </>
                )}
              </div>
              {!collapsed[f.id] && convs.map((c) => renderConv(c, true))}
              {!collapsed[f.id] && convs.length === 0 && (
                <div className="conv-empty conv-filed">empty — right-click a chat to move it here</div>
              )}
            </div>
          );
        })}
        {rootConvs.map((c) => renderConv(c, false))}
        {conversations.length === 0 && folders.length === 0 && (
          <div className="conv-empty">No conversations yet</div>
        )}
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
          {menu.owned && folders.length > 0 && (
            <>
              <div className="ctx-label">Move to</div>
              {folders.map((f) => (
                <button
                  key={f.id}
                  className="ctx-item"
                  disabled={menu.folderId === f.id}
                  onClick={() => moveTo(menu.id, f.id)}
                >
                  ▣ {f.name}
                </button>
              ))}
              {menu.folderId && (
                <button className="ctx-item" onClick={() => moveTo(menu.id, null)}>
                  ⌂ No folder
                </button>
              )}
            </>
          )}
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

      {folderMenu && (
        <div
          className="ctx-menu"
          style={{ left: folderMenu.x, top: folderMenu.y }}
          onClick={(e) => e.stopPropagation()}
        >
          <button
            className="ctx-item"
            onClick={() => {
              setRenamingFolder(folderMenu.id);
              setFolderDraft(folderMenu.name);
              setFolderMenu(null);
            }}
          >
            ✎ Rename
          </button>
          <button
            className="ctx-item ctx-danger"
            onClick={() => deleteFolder(folderMenu.id)}
            title="Chats inside move back to the list — nothing is deleted"
          >
            ✕ Delete folder
          </button>
        </div>
      )}

      {keysOpen && <KeySettings onClose={() => setKeysOpen(false)} onChanged={onKeysChanged} />}

      {dataConnOpen && <DataConnections onClose={() => setDataConnOpen(false)} />}

      {sharing && (
        <ShareDialog
          conversationId={sharing.id}
          title={sharing.title}
          onClose={() => setSharing(null)}
        />
      )}

      <div className="sidebar-footer">
        <button className="tools-toggle" onClick={toggleTools}>
          {toolsOpen ? "▾" : "▸"} Tools & settings
        </button>
        {toolsOpen && (
        <div className="tools-list">
        <button className="logout" onClick={onDashboards} style={{ marginBottom: 8 }}>
          ▦ Dashboards
        </button>
        <button className="logout" onClick={onQueries} style={{ marginBottom: 8 }}>
          ⌗ Saved SQL
        </button>
        <button className="logout" onClick={onPipelines} style={{ marginBottom: 8 }}>
          ⑃ Pipelines
        </button>
        <button className="logout" onClick={onFlow} style={{ marginBottom: 8 }}>
          ⇉ Pipeline flow
        </button>
        <button className="logout" onClick={onJobs} style={{ marginBottom: 8 }}>
          ⚙ Jobs
        </button>
        <button className="logout" onClick={onPyBuild} style={{ marginBottom: 8 }}>
          ⟨⟩ Build Python
        </button>
        <button className="logout" onClick={onToolBuilder} style={{ marginBottom: 8 }}>
          ⚇ Build tool / MCP
        </button>
        <button className="logout" onClick={onKag} style={{ marginBottom: 8 }}>
          ▤ Knowledge (KAG)
        </button>
        <button className="logout" onClick={() => setDataConnOpen(true)} style={{ marginBottom: 8 }}>
          ⧉ Data connections
        </button>
        <button className="logout" onClick={onSessions} style={{ marginBottom: 8 }}>
          ⟳ Sessions
        </button>
        <button className="logout" onClick={onAgents} style={{ marginBottom: 8 }}>
          ⚇ Agents
        </button>
        {user?.role === "admin" && (
          <button className="logout" onClick={onRedTeam} style={{ marginBottom: 8 }}>
            ◉ Red team benchmark
          </button>
        )}
        <button className="logout" onClick={onAutopilot} style={{ marginBottom: 8 }}>
          ✦ Autopilot
        </button>
        <button className="logout" onClick={onSkills} style={{ marginBottom: 8 }}>
          ▤ Skill files
        </button>
        <button className="logout" onClick={onSemantic} style={{ marginBottom: 8 }}>
          ▣ Semantic layer
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
        </div>
        )}
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
