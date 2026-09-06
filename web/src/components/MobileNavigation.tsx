import { IconChat, IconCheck, IconGraph, IconGrid, IconLayers } from "./Icon";

type Tab = "chat" | "explore" | "review" | "knowledge" | "admin" | "ontology";

interface Props {
  tab: Tab;
  tabs: string[];
  onTab: (tab: Tab) => void;
}

const labels: Record<Tab, string> = {
  chat: "Ask",
  explore: "Explore",
  review: "Review",
  knowledge: "Knowledge",
  admin: "Manage",
  ontology: "Schema",
};

function TabIcon({ tab }: { tab: Tab }) {
  if (tab === "chat") return <IconChat size={19} />;
  if (tab === "explore") return <IconGraph size={19} />;
  if (tab === "review") return <IconCheck size={19} />;
  if (tab === "knowledge") return <IconLayers size={19} />;
  return <IconGrid size={19} />;
}

/** Primary destinations stay reachable with one thumb. The workspace drawer
 * owns secondary tasks such as changing scope and reopening conversations. */
export function MobileNavigation({ tab, tabs, onTab }: Props) {
  const destinations = (["chat", "explore", "review", "knowledge"] as Tab[])
    .filter(item => tabs.includes(item));
  const management = tabs.includes("admin") ? "admin" : tabs.includes("ontology") ? "ontology" : null;
  if (management) destinations.push(management);

  return (
    <nav className="mobile-nav" aria-label="Primary navigation">
      {destinations.map(item => (
        <button
          key={item}
          type="button"
          className={tab === item ? "mobile-nav__item is-active" : "mobile-nav__item"}
          aria-current={tab === item ? "page" : undefined}
          onClick={() => onTab(item)}
        >
          <TabIcon tab={item} />
          <span>{labels[item]}</span>
        </button>
      ))}
    </nav>
  );
}
