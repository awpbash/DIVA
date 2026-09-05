import { useEffect, useRef, useState } from "react";
import {
  activateSetupDomain, apiUrl, addSetupOntologyField, buildFromScratch, chooseDemoDomain,
  chooseExistingDomain, deleteSetupOntologyField, draftSchema, editSetupOntologyField,
  finishSetup, getSetupDomains, getSetupOntology, setSetupAdmin, setSetupCategorySensitivity,
  setSetupInstance, testSetupApiKey,
} from "../api";
import type {
  DraftedField, EditFieldBody, NewFieldBody, OntologyCategory, OntologyField, OntologyResponse,
  ScratchField,
} from "../api";
import { useAuth } from "../auth";
import { OntologyEditor } from "./OntologyEditor";
import "./SetupWizard.css";

// First-run setup, entirely in the browser. Instance name, admin account,
// model API key (live-tested before it can be saved), which domain to run,
// then a restart the app triggers itself. No session exists yet — every
// /setup/* call is unauthenticated, and the backend refuses all of them the
// moment setup is already complete (see api/routes/setup.py).
//
// Four domain paths: the bundled demo corpus (zero authoring); one of the
// domains this checkout already ships AS-IS; that same starting point with
// an extra "edit" step before finishing (clone-and-edit, Phase 2); or build
// one from scratch (Phase 3) — describe the documents, let AI draft a field
// list, edit it entirely in the browser (held in this component's own state,
// nothing saved server-side until Generate), then generate and validate the
// real files in one call. See docs/setup-wizard-plan.md section 3.5/12 for
// why the from-scratch structural toggles are fixed templates, never
// freely generated.

type Step = "welcome" | "admin" | "apikey" | "domain"
  | "edit" | "scratch_describe" | "scratch_edit" | "finishing";
const STEPS: Step[] = ["welcome", "admin", "apikey", "domain", "finishing"];

const DEFAULT_DOC_TYPES = ["Agreement", "Amendment", "Amended and Restated",
  "Assignment", "Side Letter", "Notice", "Other"];

// A cheap local heuristic, not a model call: the AI's role in party-matching
// is advisory and the user confirms/corrects every suggestion anyway (see
// docs/setup-wizard-plan.md decision #12), so a keyword pre-check costs
// nothing and needs no network round trip.
const PARTY_HINT_WORDS = ["party", "parties", "company", "landlord", "tenant",
  "licensor", "licensee", "vendor", "customer", "supplier", "client",
  "counterparty", "organization", "employer", "employee", "buyer", "seller"];

interface LocalField {
  key: string; title: string; type: string; hint: string; values: string[];
  category: string; multiplicity: number; confidential: boolean;
}

const slug = (s: string): string =>
  s.trim().toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");

const categoryTitle = (key: string): string =>
  key.replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase());

export function SetupWizard({ onDone }: { onDone: () => void }) {
  const { login } = useAuth();
  const [step, setStep] = useState<Step>("welcome");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [instanceName, setInstanceName] = useState("DIVA");
  const [adminEmail, setAdminEmail] = useState("");
  const [adminName, setAdminName] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [keyVerified, setKeyVerified] = useState<string | null>(null);

  const [domains, setDomains] = useState<string[]>([]);
  const [domainChoice, setDomainChoice] =
    useState<"demo" | "existing" | "clone" | "scratch" | null>(null);
  const [pickedDomain, setPickedDomain] = useState("");

  // From-scratch (Phase 3) state. The field list is a REF as well as state:
  // OntologyEditor's mutate-then-reload pattern (see OntologyEditor.tsx)
  // expects a fresh read immediately after a mutate call resolves, and a
  // plain useState closure captured when the adapter functions were created
  // would still see the PRE-mutation value at that point (React batches the
  // re-render). The ref is always current; the state exists only to drive
  // this component's own render (the toggle panels below).
  const [scratchDomainName, setScratchDomainName] = useState("");
  const [scratchDescription, setScratchDescription] = useState("");
  const scratchFieldsRef = useRef<LocalField[]>([]);
  const [scratchFields, setScratchFieldsView] = useState<LocalField[]>([]);
  const [scratchAmendmentOn, setScratchAmendmentOn] = useState(false);
  const [scratchDocTypes, setScratchDocTypes] = useState(DEFAULT_DOC_TYPES.join("\n"));
  const [scratchPartyOn, setScratchPartyOn] = useState(false);
  const [scratchPartyKeys, setScratchPartyKeys] = useState<string[]>([]);

  function setScratchFields(next: LocalField[]) {
    scratchFieldsRef.current = next;
    setScratchFieldsView(next);
  }

  useEffect(() => {
    if (step === "domain") getSetupDomains().then(setDomains).catch(() => setDomains([]));
  }, [step]);

  const goto = (s: Step) => { setError(null); setStep(s); };

  async function submitWelcome() {
    setBusy(true); setError(null);
    try {
      await setSetupInstance(instanceName.trim() || "DIVA");
      goto("admin");
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }

  async function submitAdmin() {
    if (!adminEmail.trim()) { setError("An email address is required."); return; }
    setBusy(true); setError(null);
    try {
      await setSetupAdmin(adminEmail.trim(), adminName.trim());
      goto("apikey");
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }

  async function submitApiKey() {
    if (!apiKey.trim()) { setError("Paste your model API key to continue."); return; }
    setBusy(true); setError(null);
    try {
      const res = await testSetupApiKey(apiKey.trim(), baseUrl.trim());
      setKeyVerified(String(res.message || "Key verified."));
      goto("domain");
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }

  async function submitDomain() {
    if (domainChoice === "scratch") { goto("scratch_describe"); return; }
    setBusy(true); setError(null);
    try {
      const domain = domainChoice === "demo" ? null : (pickedDomain || domains[0]);
      if (domainChoice !== "demo" && !domain) throw new Error("No domain to choose from.");
      if (domainChoice === "demo") {
        await chooseDemoDomain();
      } else {
        await chooseExistingDomain(domain as string);
      }
      if (domainChoice === "clone") {
        // Activate the chosen domain NOW (a restart, same mechanism
        // /setup/finish uses) so the field editor below works against the
        // real, live schema instead of a preview — see api/routes/setup.py's
        // /setup/domain/activate and docs/setup-wizard-plan.md P2.1.
        await activateSetupDomain();
        await waitForRestart();
        goto("edit");
        return;
      }
      await finishAndEnter();
    } catch (e) { setError((e as Error).message); setStep("domain"); } finally { setBusy(false); }
  }

  async function finishAfterEdit() {
    setBusy(true); setError(null);
    try {
      await finishAndEnter();
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }

  // ------------------------------------------------------------------- //
  // From scratch: describe -> draft (or start blank) -> local edit -> generate
  // ------------------------------------------------------------------- //
  async function submitScratchDescribe(withDraft: boolean) {
    if (!scratchDomainName.trim()) { setError("Give this schema a short name first."); return; }
    if (withDraft && !scratchDescription.trim()) {
      setError("Describe the documents first, or start with a blank schema instead.");
      return;
    }
    setBusy(true); setError(null);
    try {
      if (withDraft) {
        const drafted: DraftedField[] = await draftSchema(scratchDescription.trim());
        setScratchFields(drafted.map(d => ({
          key: d.key, title: d.title, type: d.type, hint: d.hint,
          values: d.values, category: d.category || "details", multiplicity: 1,
          confidential: d.confidential,
        })));
      } else {
        setScratchFields([]);
      }
      setScratchPartyKeys([]);
      goto("scratch_edit");
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }

  function togglePartyMatching(on: boolean) {
    setScratchPartyOn(on);
    if (on) {
      const guessed = scratchFieldsRef.current
        .filter(f => PARTY_HINT_WORDS.some(w =>
          `${f.title} ${f.key} ${f.hint}`.toLowerCase().includes(w)))
        .map(f => f.key);
      setScratchPartyKeys(guessed);
    } else {
      setScratchPartyKeys([]);
    }
  }

  async function submitScratchGenerate() {
    if (scratchFieldsRef.current.length === 0) {
      setError("Add at least one field before generating.");
      return;
    }
    setBusy(true); setError(null);
    try {
      const fields: ScratchField[] = scratchFieldsRef.current.map(f => ({
        key: f.key, title: f.title, type: f.type, hint: f.hint,
        values: f.values, category: f.category, multiplicity: f.multiplicity,
        confidential: f.confidential,
      }));
      await buildFromScratch({
        domain: scratchDomainName.trim(),
        fields,
        amendment: {
          enabled: scratchAmendmentOn,
          document_types: scratchAmendmentOn
            ? scratchDocTypes.split("\n").map(s => s.trim()).filter(Boolean)
            : undefined,
        },
        party: { enabled: scratchPartyOn, field_keys: scratchPartyOn ? scratchPartyKeys : undefined },
      });
      await finishAndEnter();
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }

  // Adapters: OntologyEditor talks to these exactly like the real /ontology/*
  // and /setup/ontology/* calls elsewhere in this file — same props, same
  // shape — but nothing here touches the network. There is no active domain
  // yet to persist an overlay onto (that's what clone-and-edit's version of
  // this screen has and this one doesn't); everything lives in scratchFieldsRef
  // until Generate turns it into real files.
  async function localFetchOntology(): Promise<OntologyResponse> {
    const byCat = new Map<string, OntologyField[]>();
    for (const f of scratchFieldsRef.current) {
      const list = byCat.get(f.category) ?? [];
      list.push({
        key: f.key, full_key: `${f.category}.${f.key}`, title: f.title, type: f.type,
        values: f.values, hint: f.hint,
        sensitivity: f.confidential ? "confidential" : "general",
        mechanism: "llm", multiplicity: f.multiplicity, origin: "user", user_field: true,
      });
      byCat.set(f.category, list);
    }
    const categories: OntologyCategory[] = Array.from(byCat.entries()).map(([key, fields]) => ({
      key, title: categoryTitle(key), level: "general", fields,
    }));
    return { categories, default_level: "general", can_edit: true };
  }

  async function localAddField(body: NewFieldBody): Promise<void> {
    if (scratchFieldsRef.current.some(f => f.key === body.key)) {
      throw new Error(`the field key "${body.key}" is already used — keys must be unique.`);
    }
    setScratchFields([...scratchFieldsRef.current, {
      key: body.key, title: body.title, type: body.type, hint: body.hint ?? "",
      values: body.values ?? [], category: body.category,
      multiplicity: body.multiplicity ?? 1, confidential: false,
    }]);
  }

  async function localEditField(category: string, key: string, body: EditFieldBody): Promise<void> {
    setScratchFields(scratchFieldsRef.current.map(f => (f.category !== category || f.key !== key) ? f : {
      ...f,
      title: body.title ?? f.title,
      hint: body.hint ?? f.hint,
      values: body.values ?? f.values,
      confidential: body.sensitivity ? body.sensitivity === "confidential" : f.confidential,
    }));
  }

  async function localDeleteField(category: string, key: string): Promise<void> {
    setScratchFields(scratchFieldsRef.current.filter(f => !(f.category === category && f.key === key)));
    setScratchPartyKeys(prev => prev.filter(k => k !== key));
  }

  async function localSetSensitivity(category: string, level: string): Promise<void> {
    setScratchFields(scratchFieldsRef.current.map(f =>
      f.category !== category ? f : { ...f, confidential: level === "confidential" }));
  }

  /** Shared tail for every path once its domain is settled: mark setup
   * complete, restart, wait for the process to come back, sign the wizard's
   * own new admin straight in, and hand off to the real app. */
  async function finishAndEnter() {
    await finishSetup();
    goto("finishing");
    await waitForRestart();
    // The wizard's own admin step just created this account — sign
    // straight into it so finishing setup and landing in the app feel
    // like one continuous action, no separate login screen in between.
    try {
      await login(adminEmail.trim());
      sessionStorage.setItem("verbatim.postSetupLandTab", "admin");
    } catch { /* the restarted process may still be settling — fall back
                to the ordinary login screen, which now shows the same
                account as its first-run sign-in hint */ }
    onDone();
  }

  async function waitForRestart(): Promise<void> {
    // The process we're talking to is about to exit and come back. A few
    // failed requests while it's down are expected, not an error.
    await sleep(1500);
    for (let i = 0; i < 60; i++) {
      try {
        const r = await fetch(apiUrl("/healthz"));
        if (r.ok) return;
      } catch { /* still restarting */ }
      await sleep(1500);
    }
  }

  const stepIndex = STEPS.indexOf(step);

  if (step === "edit" || step === "scratch_edit") {
    const scratch = step === "scratch_edit";
    return (
      <div className="setup">
        <div className="setup__editshell">
          <div className="setup__edit-head">
            <div className="setup__brand"><div className="setup__mark" /><span>Set up this instance</span></div>
            <div className="setup__eyebrow">
              {scratch ? `Building "${scratchDomainName || "your schema"}"` : "Before you begin"}
            </div>
            <h1>{scratch ? "Review and refine the fields" : "Edit your schema"}</h1>
          </div>
          <div className={"setup__edit-body" + (scratch ? " setup__edit-body--scratch" : "")}>
            {scratch && (
              <div className="setup__toggles">
                <label className="setup__toggle">
                  <input type="checkbox" checked={scratchAmendmentOn}
                        onChange={e => setScratchAmendmentOn(e.target.checked)} disabled={busy} />
                  <div>
                    <div className="setup__toggle-title">Chain of related documents</div>
                    <div className="setup__toggle-desc">
                      Turn this on if documents amend or supersede earlier ones —
                      contracts and their amendments, for example.
                    </div>
                  </div>
                </label>
                {scratchAmendmentOn && (
                  <div className="setup__toggle-detail">
                    <label>Document types (one per line)
                      <textarea value={scratchDocTypes} rows={3} disabled={busy}
                               onChange={e => setScratchDocTypes(e.target.value)} />
                    </label>
                  </div>
                )}
                <label className="setup__toggle">
                  <input type="checkbox" checked={scratchPartyOn}
                        onChange={e => togglePartyMatching(e.target.checked)} disabled={busy} />
                  <div>
                    <div className="setup__toggle-title">Match the same company or person across documents</div>
                    <div className="setup__toggle-desc">
                      Pick which fields below name a party, so mentions of the
                      same one link up automatically. Pre-checked from your
                      field names — review before generating.
                    </div>
                  </div>
                </label>
                {scratchPartyOn && (
                  <div className="setup__toggle-detail setup__toggle-fields">
                    {scratchFields.length === 0 && <p className="setup__hint">Add a field first.</p>}
                    {scratchFields.map(f => (
                      <label key={f.key} className="setup__toggle-check">
                        <input type="checkbox" checked={scratchPartyKeys.includes(f.key)} disabled={busy}
                              onChange={() => setScratchPartyKeys(prev =>
                                prev.includes(f.key) ? prev.filter(k => k !== f.key) : [...prev, f.key])} />
                        {f.title}
                      </label>
                    ))}
                  </div>
                )}
              </div>
            )}
            <OntologyEditor
              title={scratch ? "Fields" : "Your schema"}
              fetchOntology={scratch ? localFetchOntology : getSetupOntology}
              addField={scratch ? localAddField : addSetupOntologyField}
              editField={scratch ? localEditField : editSetupOntologyField}
              deleteField={scratch ? localDeleteField : deleteSetupOntologyField}
              setSensitivity={scratch ? localSetSensitivity : setSetupCategorySensitivity}
            />
          </div>
          {error && <div className="setup__error setup__edit-error">{error}</div>}
          <div className="setup__edit-actions">
            <button className="setup__back"
                   onClick={() => goto(scratch ? "scratch_describe" : "domain")} disabled={busy}>
              {scratch ? "Back" : "Back to domain choice"}
            </button>
            <button className="setup__primary" onClick={scratch ? submitScratchGenerate : finishAfterEdit} disabled={busy}>
              {busy ? (scratch ? "Generating…" : "Finishing…") : (scratch ? "Generate and finish" : "Finish setup")}
            </button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="setup">
      <div className="setup__card">
        <div className="setup__brand"><div className="setup__mark" /><span>Set up this instance</span></div>
        <div className="setup__steps">
          {STEPS.map((s, i) => (
            <div key={s} className={
              "setup__step-dot" + (i < stepIndex ? " setup__step-dot--done"
                : i === stepIndex ? " setup__step-dot--active" : "")
            } />
          ))}
        </div>

        {step === "welcome" && (
          <>
            <h1>Welcome</h1>
            <p className="setup__sub">
              A few short steps: name this instance, create your admin account,
              connect a model, and choose what it reads. Then it restarts
              itself and you're in.
            </p>
            <div className="setup__field">
              <label htmlFor="setup-name">What should this instance be called?</label>
              <input id="setup-name" value={instanceName} autoFocus
                    onChange={e => setInstanceName(e.target.value)} disabled={busy} />
            </div>
            {error && <div className="setup__error">{error}</div>}
            <div className="setup__actions">
              <span />
              <button className="setup__primary" onClick={submitWelcome} disabled={busy}>
                {busy ? "Saving…" : "Continue"}
              </button>
            </div>
          </>
        )}

        {step === "admin" && (
          <>
            <div className="setup__eyebrow">Step 2 of 4</div>
            <h1>Your admin account</h1>
            <p className="setup__sub">
              Sign-in here is passwordless — this email address is the whole
              credential, so keep this instance private or put it behind
              something that authenticates. This replaces the temporary
              default account this checkout starts with.
            </p>
            <div className="setup__field">
              <label htmlFor="setup-admin-email">Your email</label>
              <input id="setup-admin-email" type="email" value={adminEmail} autoFocus
                    onChange={e => setAdminEmail(e.target.value)} disabled={busy}
                    placeholder="you@company.com" />
            </div>
            <div className="setup__field">
              <label htmlFor="setup-admin-name">Your name</label>
              <input id="setup-admin-name" value={adminName}
                    onChange={e => setAdminName(e.target.value)} disabled={busy}
                    placeholder="Optional" />
            </div>
            {error && <div className="setup__error">{error}</div>}
            <div className="setup__actions">
              <button className="setup__back" onClick={() => goto("welcome")} disabled={busy}>Back</button>
              <button className="setup__primary" onClick={submitAdmin} disabled={busy}>
                {busy ? "Saving…" : "Continue"}
              </button>
            </div>
          </>
        )}

        {step === "apikey" && (
          <>
            <div className="setup__eyebrow">Step 3 of 4</div>
            <h1>Connect a model</h1>
            <p className="setup__sub">
              Documents are read and answered by an OpenAI-compatible model.
              We'll make one small test call before saving this, so a typo
              doesn't surface later as a broken instance.
            </p>
            <div className="setup__field">
              <label htmlFor="setup-key">API key</label>
              <input id="setup-key" type="password" value={apiKey} autoFocus
                    onChange={e => setApiKey(e.target.value)} disabled={busy}
                    placeholder="sk-…" />
            </div>
            <details className="setup__advanced">
              <summary>Advanced: use a different endpoint</summary>
              <div className="setup__advanced-body">
                <div className="setup__field">
                  <label htmlFor="setup-base-url">Model base URL</label>
                  <input id="setup-base-url" value={baseUrl}
                        onChange={e => setBaseUrl(e.target.value)} disabled={busy}
                        placeholder="Leave blank for api.openai.com" />
                  <div className="setup__hint">
                    Any OpenAI-compatible endpoint works — Azure AI Foundry, a
                    local server, a gateway.
                  </div>
                </div>
              </div>
            </details>
            {error && <div className="setup__error">{error}</div>}
            <div className="setup__actions">
              <button className="setup__back" onClick={() => goto("admin")} disabled={busy}>Back</button>
              <button className="setup__primary" onClick={submitApiKey} disabled={busy}>
                {busy ? "Testing…" : "Test and continue"}
              </button>
            </div>
          </>
        )}

        {step === "domain" && (
          <>
            <div className="setup__eyebrow">Step 4 of 4</div>
            <h1>What will it read?</h1>
            {keyVerified && <div className="setup__ok">{keyVerified}</div>}
            <p className="setup__sub">
              Pick how to start. You can refine the field schema later from
              the Ontology tab once you're in.
            </p>
            <div className="setup__paths">
              <button
                type="button"
                className={"setup__path" + (domainChoice === "demo" ? " setup__path--selected" : "")}
                onClick={() => setDomainChoice("demo")}
              >
                <div className="setup__path-title">Try it with sample contracts</div>
                <div className="setup__path-desc">
                  Loads a ready-made schema for commercial agreements plus
                  three example contracts, so you can ask real questions
                  immediately.
                </div>
              </button>
              <button
                type="button"
                className={"setup__path" + (domainChoice === "existing" ? " setup__path--selected" : "")}
                onClick={() => { setDomainChoice("existing"); setPickedDomain(domains[0] || ""); }}
              >
                <div className="setup__path-title">Use a schema this checkout already has</div>
                <div className="setup__path-desc">
                  Start from an existing field schema, unchanged, and upload
                  your own documents against it.
                </div>
              </button>
              <button
                type="button"
                className={"setup__path" + (domainChoice === "clone" ? " setup__path--selected" : "")}
                onClick={() => { setDomainChoice("clone"); setPickedDomain(domains[0] || ""); }}
              >
                <div className="setup__path-title">Start from a schema, then edit it</div>
                <div className="setup__path-desc">
                  Same starting point, but add, rename or remove fields
                  before you begin uploading.
                </div>
              </button>
              <button
                type="button"
                className={"setup__path" + (domainChoice === "scratch" ? " setup__path--selected" : "")}
                onClick={() => setDomainChoice("scratch")}
              >
                <div className="setup__path-title">Build a new schema</div>
                <div className="setup__path-desc">
                  Describe your documents in plain language and let AI draft
                  a starting field list for you to edit.
                </div>
              </button>
            </div>
            {(domainChoice === "existing" || domainChoice === "clone") && domains.length > 1 && (
              <div className="setup__field">
                <label htmlFor="setup-domain-pick">Which one</label>
                <select id="setup-domain-pick" value={pickedDomain}
                       onChange={e => setPickedDomain(e.target.value)} disabled={busy}>
                  {domains.map(d => <option key={d} value={d}>{d}</option>)}
                </select>
              </div>
            )}
            {error && <div className="setup__error">{error}</div>}
            <div className="setup__actions">
              <button className="setup__back" onClick={() => goto("apikey")} disabled={busy}>Back</button>
              <button className="setup__primary" onClick={submitDomain} disabled={busy || !domainChoice}>
                {busy
                  ? (domainChoice === "clone" ? "Activating…" : "Finishing…")
                  : (domainChoice === "clone" || domainChoice === "scratch" ? "Continue" : "Finish setup")}
              </button>
            </div>
          </>
        )}

        {step === "scratch_describe" && (
          <>
            <div className="setup__eyebrow">Build a new schema</div>
            <h1>Describe your documents</h1>
            <p className="setup__sub">
              A short name and a plain-language description of what these
              documents are. AI drafts a starting field list from it — you'll
              review and edit every field before anything is saved.
            </p>
            <div className="setup__field">
              <label htmlFor="setup-scratch-name">Short name for this schema</label>
              <input id="setup-scratch-name" value={scratchDomainName} autoFocus
                    onChange={e => setScratchDomainName(e.target.value)} disabled={busy}
                    placeholder="e.g. Lease Agreements" />
            </div>
            <div className="setup__field">
              <label htmlFor="setup-scratch-desc">What are these documents?</label>
              <textarea id="setup-scratch-desc" value={scratchDescription} rows={5} disabled={busy}
                       onChange={e => setScratchDescription(e.target.value)}
                       placeholder="e.g. Commercial lease agreements between a landlord and a tenant, covering rent, term length, and renewal options." />
              <div className="setup__hint">
                Or{" "}
                <button type="button" className="setup__inline-link" disabled={busy}
                       onClick={() => submitScratchDescribe(false)}>
                  start with a blank schema
                </button>{" "}
                and add fields by hand.
              </div>
            </div>
            {error && <div className="setup__error">{error}</div>}
            <div className="setup__actions">
              <button className="setup__back" onClick={() => goto("domain")} disabled={busy}>Back</button>
              <button className="setup__primary" onClick={() => submitScratchDescribe(true)} disabled={busy}>
                {busy ? "Drafting…" : "Draft with AI"}
              </button>
            </div>
          </>
        )}

        {step === "finishing" && (
          <div className="setup__finishing">
            <div className="setup__spinner" />
            <h1>Setting up…</h1>
            <p>Restarting with your configuration. This takes a few seconds.</p>
          </div>
        )}
      </div>
    </div>
  );
}

const sleep = (ms: number) => new Promise<void>(res => setTimeout(res, ms));
