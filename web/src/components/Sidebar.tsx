import { Account } from "../api";
import { useBranding } from "../branding";
import { DocumentMeta } from "../types";
import { DocCatalog } from "./DocCatalog";
import { IconChat, IconGraph, IconPlus, IconTrash } from "./Icon";

type Tab = "chat" | "explore" | "review" | "knowledge" | "admin" | "ontology";

interface RecentThread {
  id: string;
  title: string;
}

interface Props {
  tab: Tab;
  onTab: (t: Tab) => void;
  docs: DocumentMeta[];
  /** Contracts in the search scope. Empty = all (cross-doc). */
  selectedDocIds: string[];
  viewerDocId: string | null;
  onToggleDocScope: (id: string) => void;
  onClearScope: () => void;
  onOpenDoc: (id: string) => void;
  threads: RecentThread[];
  activeThreadId: string | null;
  onSelectThread: (id: string) => void;
  onDeleteThread: (id: string) => void;
  onClearConversations: () => void;
  onNewChat: () => void;
  /** The logged-in account — drives RBAC + shown as an identity chip. */
  user: Account | null;
  onLogout: () => void;
  /** Tabs this account may use — server-declared (auth capabilities). */
  tabs: string[];
}

export function Sidebar(props: Props) {
  const can = (t: Tab) => props.tabs.includes(t);
  const { appName } = useBranding();
  return (
    <aside className="sidebar">
      <div className="sidebar__brand">
        <div className="sidebar__brand-mark" />
        <span>{appName}</span>
      </div>

      <button className="sidebar__new" onClick={props.onNewChat}>
        <IconPlus />
        <span>New chat</span>
      </button>

      <nav className="sidebar__nav" style={{ marginTop: 12 }}>
        <button
          className="sidebar__nav-item"
          aria-current={props.tab === "chat"}
          onClick={() => props.onTab("chat")}
        >
          <IconChat />
          <span>Chat</span>
        </button>
        {can("explore") && (
          <button
            className="sidebar__nav-item"
            aria-current={props.tab === "explore"}
            onClick={() => props.onTab("explore")}
          >
            <IconGraph />
            <span>Explore graph</span>
          </button>
        )}
        {can("review") && (
          <button
            className="sidebar__nav-item"
            aria-current={props.tab === "review"}
            onClick={() => props.onTab("review")}
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                 strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M9 11l3 3L22 4" />
              <path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11" />
            </svg>
            <span>Review</span>
          </button>
        )}
        {can("knowledge") && (
          <button
            className="sidebar__nav-item"
            aria-current={props.tab === "knowledge"}
            onClick={() => props.onTab("knowledge")}
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                 strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <ellipse cx="12" cy="5" rx="9" ry="3" />
              <path d="M3 5v14a9 3 0 0 0 18 0V5" />
              <path d="M3 12a9 3 0 0 0 18 0" />
            </svg>
            <span>Knowledge</span>
          </button>
        )}
        {can("ontology") && (
          <button
            className="sidebar__nav-item"
            aria-current={props.tab === "ontology"}
            onClick={() => props.onTab("ontology")}
            title="Manage the fields the AI looks for, and which categories are confidential"
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                 strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M12 3v18" />
              <rect x="3" y="3" width="18" height="18" rx="2" />
              <path d="M3 9h18" />
            </svg>
            <span>Ontology</span>
          </button>
        )}
        {can("admin") && (
          <button
            className="sidebar__nav-item"
            aria-current={props.tab === "admin"}
            onClick={() => props.onTab("admin")}
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                 strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <rect x="3" y="3" width="7" height="7" rx="1" />
              <rect x="14" y="3" width="7" height="7" rx="1" />
              <rect x="3" y="14" width="7" height="7" rx="1" />
              <rect x="14" y="14" width="7" height="7" rx="1" />
            </svg>
            <span>Admin</span>
          </button>
        )}
      </nav>

      <div className={`user-chip user-chip--${props.user?.role ?? "default"}`}>
        <div className="user-chip__info">
          <span className="user-chip__name" title={props.user?.title || props.user?.email}>
            {props.user?.name || props.user?.email}
          </span>
          <span className="user-chip__role">{props.user?.role} · {props.user?.role === "default" ? "sensitive data hidden" : "full access"}</span>
        </div>
        <button className="user-chip__logout" onClick={props.onLogout} title="Sign out" aria-label="Sign out">⏻</button>
      </div>

      <DocCatalog
        docs={props.docs}
        selectedDocIds={props.selectedDocIds}
        viewerDocId={props.viewerDocId}
        onToggleScope={props.onToggleDocScope}
        onClearScope={props.onClearScope}
        onOpenDoc={props.onOpenDoc}
      />

      <div className="sidebar__section-label">
        <span>Recent</span>
        {props.threads.length > 0 && (
          <button
            type="button"
            className="sidebar__clear"
            onClick={() => {
              if (window.confirm("Delete all conversations? This can't be undone.")) {
                props.onClearConversations();
              }
            }}
            title="Delete all conversations"
          >
            Clear
          </button>
        )}
      </div>
      <div className="sidebar__threads">
        {props.threads.length === 0 ? (
          <div className="sidebar__empty">No conversations yet. Ask a question to start one.</div>
        ) : (
          props.threads.map(t => (
            <div
              key={t.id}
              className={`sidebar__thread-row ${t.id === props.activeThreadId ? "sidebar__thread-row--active" : ""}`}
            >
              <button
                className="sidebar__thread"
                onClick={() => props.onSelectThread(t.id)}
                title={t.title}
              >
                {t.title}
              </button>
              <button
                className="sidebar__thread-del"
                onClick={e => {
                  e.stopPropagation();
                  if (window.confirm(`Delete "${t.title}"?`)) {
                    props.onDeleteThread(t.id);
                  }
                }}
                title="Delete chat"
                aria-label="Delete chat"
              >
                <IconTrash size={13} />
              </button>
            </div>
          ))
        )}
      </div>
    </aside>
  );
}
