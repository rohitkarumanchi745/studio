import { clearSession, getUser } from "../api";

export default function Sidebar({ conversations, activeId, onSelect, onNew, onDelete, onActivity, onCollapse }) {
  const user = getUser();
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
            onClick={() => onSelect(c.id)}
          >
            <span className="conv-title">{c.title}</span>
            <button
              className="conv-del"
              title="Delete"
              onClick={(e) => {
                e.stopPropagation();
                onDelete(c.id);
              }}
            >
              ×
            </button>
          </div>
        ))}
        {conversations.length === 0 && <div className="conv-empty">No conversations yet</div>}
      </div>
      <div className="sidebar-footer">
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
