import { useEffect, useMemo, useState } from "react";
import { useAuth } from "../auth";
import "./HelpButton.css";

// Floating "How to use" guide — a round "?" (bottom-right) that opens a
// walkthrough modal. Sections only appear for tabs the server granted this
// account, and the access-level list marks the viewer's own level. Auto-opens
// once per browser + account (localStorage flag keyed by email), then only on
// demand.
//
// Keep the copy domain-neutral. This ships to every deployment, so it can
// describe what a screen DOES but never what any one corpus contains.

const seenKey = (email: string | null | undefined) =>
  `verbatim.help_seen.${email || "anon"}`;

const ROLE_LINES: { role: string; name: string; text: string }[] = [
  { role: "admin",        name: "Admin",        text: "sees everything, plus review and management tools." },
  { role: "confidential", name: "Confidential", text: "cleared for the field categories your schema marks confidential." },
  { role: "default",      name: "Default",      text: "confidential values stay hidden or blurred." },
];

interface GuideSection {
  key: string;
  title: string;
  /** Optional screenshot, served from `web/public/guide/<img>.png`.
   *
   * No section sets it, and that is deliberate rather than unfinished. A
   * screenshot of this guide's subject shows a real corpus, so the set that
   * used to ship carried one deployment's product name and its counterparties
   * into every copy of the framework. The steps below carry the instruction on
   * their own.
   *
   * An instance that wants illustrated help should capture its OWN screens
   * against its own documents and set this. That is the only version that is
   * both accurate and safe to show its users. `docs/images/` has the set the
   * documentation uses, captured against the synthetic sample corpus, as a
   * reference for framing and size. */
  img?: string;
  alt?: string;
  intro?: string;
  steps?: string[];
  note?: string;
}

export function HelpButton() {
  const { user, tabs } = useAuth();
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState("ask");

  // First visit for this account on this browser → open the guide once.
  useEffect(() => {
    if (!user) return;
    try {
      if (!localStorage.getItem(seenKey(user.email))) setOpen(true);
    } catch { /* no storage available — just stay closed */ }
  }, [user]);

  // Any open (auto or manual) counts as seen — never auto-open again.
  useEffect(() => {
    if (!open || !user) return;
    try { localStorage.setItem(seenKey(user.email), "1"); } catch { /* ignore */ }
  }, [open, user]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  const has = (t: string) => tabs.includes(t);
  const role = user?.role ?? "default";

  const sections = useMemo<GuideSection[]>(() => {
    const s: GuideSection[] = [
      {
        key: "ask", title: "Ask questions",
        intro: "Chat is the front door. Every answer comes straight from the documents and cites its source.",
        steps: [
          "Type a question and press Enter. Plain language works: amounts, dates, parties, obligations.",
          "Each statement in the answer carries a citation chip naming the clause it came from.",
          "The document picker narrows the search to specific documents. Empty means search everything.",
        ],
        note: "The assistant never guesses. “Not in the documents” is a real answer, not a failure.",
      },
      {
        key: "source", title: "Check the source",
        steps: [
          "Click any citation chip in an answer.",
          "The source PDF opens on the right with the exact clause highlighted, so you can confirm the answer against the wording in seconds.",
        ],
      },
    ];
    if (has("admin")) {
      s.push({
        key: "upload", title: "Upload and extract",
        intro: "Admins add new documents here: Admin tab, then Upload document.",
        steps: [
          "Drop the PDF or click to browse.",
          "Declare what you know: which folder it belongs in, its type and date, and whether it amends an earlier document. These details come from you, not the AI, and they are what anchor it to the right family.",
          "Upload + extract reads the document in the background (uses AI credits). It becomes searchable right away and joins the review queue.",
        ],
      });
    }
    if (has("review")) {
      s.push({
        key: "review", title: "Verify extracted fields",
        intro: "Extraction is automatic, trust is human. Review is where a person confirms each value against the document it came from.",
        steps: [
          "Pick a field from the list on the right.",
          "Read the highlighted clause on the page: that is the exact evidence behind the value.",
          "Approve it, reject it, or propose a correction.",
          "The coloured counters at the top triage the work: confident, to check, needs attention, blank.",
        ],
        note: "A verified value is marked human-verified everywhere: in chat answers, in the knowledge base, and in exports.",
      });
    }
    if (has("knowledge")) {
      s.push({
        key: "knowledge", title: "Knowledge base",
        intro: "One aligned view per document family, so amendments and originals read as a single story.",
        steps: [
          "Pick a document family at the top.",
          "The chain shows which document amends which.",
          "Every field shows its current value, with older superseded values struck through underneath.",
          "Click a value to see its clause evidence on the right.",
        ],
      });
    }
    if (has("explore")) {
      s.push({
        key: "explore", title: "Explore graph",
        intro: "A visual map of everything extracted from your documents. Click a node to inspect it and trace it back to its source page.",
      });
    }
    if (has("ontology")) {
      s.push({
        key: "ontology", title: "Ontology",
        intro: "The field schema behind extraction: what the AI looks for in every document. Add or edit fields here. Changes apply to documents extracted after the edit, so re-extract anything already read if you want the new field filled.",
      });
    }
    s.push({
      key: "access", title: "Access levels",
      intro: "Your schema marks some field categories confidential. Those values are hidden or blurred unless your account is cleared for them. A locked chip or notice means the data exists but is restricted at your level.",
    });
    s.push({
      key: "feedback", title: "Send feedback",
      steps: [
        "Pick a category: interface, extraction, wrong answer, or other.",
        "Describe what happened in a sentence or two.",
        "Attach a screenshot if it helps, then send. Reports go straight to the developer with your account and current tab already noted.",
      ],
      note: "Under any chat reply, “Report this answer” flags that specific response.",
    });
    return s;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tabs]);

  const jump = (key: string) => {
    setActive(key);
    document.getElementById(`help-sec-${key}`)
      ?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  return (
    <>
      {open && (
        <div className="helpm__backdrop" onMouseDown={e => { if (e.target === e.currentTarget) setOpen(false); }}>
          <div className="helpm" role="dialog" aria-label="How to use">
            <div className="helpm__head">
              <span>How to use</span>
              <button className="helpm__close" onClick={() => setOpen(false)} aria-label="Close">×</button>
            </div>
            <div className="helpm__layout">
              <nav className="helpm__nav" aria-label="Guide sections">
                {sections.map(s => (
                  <button
                    key={s.key}
                    className={`helpm__nav-item${active === s.key ? " is-active" : ""}`}
                    onClick={() => jump(s.key)}
                  >
                    {s.title}
                  </button>
                ))}
              </nav>
              <div className="helpm__body">
                {sections.map(s => (
                  <section key={s.key} id={`help-sec-${s.key}`} className="helpm__section">
                    <h4>{s.title}</h4>
                    {s.intro && <p>{s.intro}</p>}
                    {s.key === "access" && (
                      <ul className="helpm__roles">
                        {ROLE_LINES.map(r => (
                          <li key={r.role}>
                            <b>{r.name}</b>: {r.text}
                            {role === r.role && <span className="helpm__you">your level</span>}
                          </li>
                        ))}
                      </ul>
                    )}
                    {s.steps && (
                      <ol className="helpm__steps">
                        {s.steps.map((step, i) => (
                          <li key={i}>
                            <span className="helpm__badge" aria-hidden>{i + 1}</span>
                            <span>{step}</span>
                          </li>
                        ))}
                      </ol>
                    )}
                    {s.img && (
                      <img
                        className="helpm__shot"
                        src={`/guide/${s.img}.png`}
                        alt={s.alt || s.title}
                        loading="lazy"
                      />
                    )}
                    {s.note && <p className="helpm__note">{s.note}</p>}
                  </section>
                ))}
              </div>
            </div>
          </div>
        </div>
      )}
      <div className="help">
        <button
          className="help__fab"
          onClick={() => setOpen(o => !o)}
          type="button"
          title="How to use"
          aria-label="How to use"
          aria-expanded={open}
        >
          ?
        </button>
      </div>
    </>
  );
}
