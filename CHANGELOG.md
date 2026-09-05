# Changelog

Notable changes, newest first. This project follows [semantic versioning](https://semver.org).

## [0.3.0] - unreleased

The first version shaped as a framework rather than one deployment. Everything
below is unreleased: no tag exists yet, because the repository has not been
published. `pyproject.toml`, `api/__init__.py` and `web/package.json` all carry
`0.3.0`, and `tests/test_version.py` keeps them in step.

### Added

- A `scale` figure in `docs/diagrams/`, the flagship demo's single three-document
  chain beside the real-world corpus's six independent chains drawn to their
  real length, plus a genuine screenshot (`docs/images/real-world-citation.png`)
  of a citation answered and traced against the real-world corpus. A `README`
  complexity table gives a rough sense of where time, disk and tokens go,
  measured against both corpora rather than guessed.
- `examples/real_world/`: 19 real contracts across 6 real amendment chains,
  sourced from the CUAD dataset (public SEC EDGAR filings, CC-BY-4.0
  curation, see `NOTICE`). Opt-in via `python -m scripts.load_real_world_corpus`,
  separate from and does not touch the flagship 3-document demo. Built to
  stress-test the same amendment-chain walk the demo proves, at real-world
  scale and with genuine legal drafting instead of synthetic text.
- A documentation set rather than one long README: a getting-started
  walkthrough, a concepts page explaining why the design is shaped this way, a
  guide to teaching it your own documents, an architecture map, a settings and
  command and endpoint reference, and a troubleshooting page. Indexed by
  `docs/README.md`.
- `examples/`: three synthetic contracts and ten questions with their expected
  answers. The corpus is arranged so that answering correctly needs the
  per-field supersedence walk, because no single reading rule gets both the
  current fee and the current term right. `examples/make_corpus.py` regenerates
  the PDFs, so the fixture is reviewable text rather than three binaries.
- Generated figures in `docs/diagrams/`, one claim each, in light and dark
  variants from a single source, plus an animation of the supersedence walk.
  `make_diagrams.py` and `supersedence_anim.py` are the source and
  `docs/diagrams/README.md` covers regenerating them.
- `tests/test_docs.py` walks every link and image from the two documentation
  entry points and fails on anything that does not resolve, plus figures
  without alt text, custom heading ids GitHub does not support, and links to
  anchors that do not exist.
- Product screenshots in `docs/images/`, every one captured against the
  synthetic sample corpus. That is the rule: a screenshot of this product shows
  whatever corpus is loaded, so one taken on a real deployment carries that
  customer's document titles into every copy of the repository.
- `REVIEW_MIN_APPROVALS` and `REVIEW_CORRECTION_APPROVALS`. How many verifiers
  a value needs, and how many a *corrected* value needs, are now settings
  rather than constants.

- `python -m scripts.setup` takes a fresh checkout to a working instance: it
  creates `.env`, resolves the active domain, compiles the configuration,
  creates the database and container, and creates the first admin account.
  `--check` reports without changing anything. Everything it does is free,
  and `--check-models` adds one small paid request that proves the model
  endpoint answers.
- `python -m scripts.accounts` manages login accounts from the command line,
  for the cases a browser cannot help with.
- `GET /branding` serves the instance name, tagline, active domain, and
  version. The frontend reads it at boot, so renaming an instance is an
  environment variable and a restart rather than a rebuild.
- MIT licence, contributing guide, security policy, code of conduct, issue and
  pull request templates, and a CI workflow that runs lint, the test suite, the
  frontend build, and a Docker image build.
- Lint configuration in `pyproject.toml`, scoped to correctness rules only.
- `docker-compose.prod.yml` and `docs/DEPLOYMENT.md`: a production stack that
  binds to loopback, drops the development source mounts, rotates its logs, and
  a guide covering the reverse proxy, backups, updating and sizing.
- The container runs as a non-root user.
- The app logs its security posture on every boot, so an operator can see from
  the log alone whether anything is authenticating requests.
- `python -m scripts.export_oss`, a maintainer-only tool that never ships in
  this checkout, copies the shippable subset of a private working tree into a
  clean one before a release: allowlist, independent denylist, a content scan
  for private names and key-shaped strings, and a self-check that the result
  resolves one domain with a complete config set. Nothing a contributor working
  from this repo needs to run.
- Contract tests (`*_contract.py`) hold the pack, field-schema and ontology
  contracts over every domain a checkout ships, so the drift gates keep working
  in a copy that carries a different domain.

### Fixed

- **A party name containing a slash broke the knowledge graph build for
  every document processed afterward, not just that one.** A real contract's
  party is commonly named "X f/k/a Y" ("formerly known as") or "d/b/a"
  ("doing business as"). The literal slash landed straight in that party's
  internal database record id, which Cosmos DB rejects with a 400, so every
  later `build_km` call in the same process failed the same way. Found while
  loading `examples/real_world/`, a synthetic corpus never contains this kind
  of name. `pipeline/kb/writers.py`'s `_canonical_key` now strips all
  punctuation from a party's key, not only commas and periods.
- **The Docker image never copied `examples/` into itself.** Every
  containerized deployment's Demo button (`api/main.py`'s
  `_load_demo_corpus`) silently did nothing, since the files it looks for
  were never in the image. Added to the `Dockerfile`.
- **The Docker image never shipped `docs/` either.** `.dockerignore` excluded
  it outright, alongside genuinely dev-only material like `tests/` and the
  paper source. A deployed container could not answer its own "what does this
  button do" from its own filesystem. `docs/` is now allowed into the build
  context and copied into the image.
- **A verifier's correction did not move the current value until somebody ran
  a full rebuild.** "Not Stated" is how a person says a document does not state
  a field, which hands the current value back to an earlier document in the
  chain. The vote path stamped only the node it edited, so the corrected
  document went on being marked current and went on being marked as having
  superseded the value below it: the correction appeared to do nothing. Found
  by running the sample corpus and correcting a real misread. The walk now
  re-runs for that one field on every vote (`km.refresh_field_currency`), and
  the build stamps each document's chain position so the targeted walk orders
  the family exactly as the full build does.
- **A blank `OPENAI_BASE_URL` disabled the default endpoint.** The OpenAI SDK
  reads that variable whenever no base URL is passed and treats
  present-but-empty as set, so a `.env` carrying the line with nothing after
  the equals sign pointed every model call at an empty host. The only symptom
  was `Request URL is missing an 'http://' or 'https://' protocol` from inside
  the HTTP library, which names nothing an operator can act on. The endpoint is
  now always passed explicitly.
- **A fresh install had no way to learn the account it had created.** Sign-in
  is passwordless and the login screen deliberately does not enumerate
  accounts, so the operator of a new instance faced an empty box and an address
  documented somewhere else. The screen now names the bootstrap account, and
  only while the instance has never been signed into by anyone.
- **`docker compose up -d` alone produced an instance that never became
  ready.** Nothing created the database except a setup command the operator had
  not necessarily run, so the login screen sat on "waiting for the knowledge
  store" indefinitely. The app now creates its own database and container at
  boot, idempotently, on a thread so a cold database never blocks startup.
- **No model key crashed the app at import**, which meant a crash-looping
  container and a browser that could not connect, with the real reason buried
  in the logs. The app now starts, says so in the log and in `/readyz`, and
  fails with one clear sentence at the point a model is actually needed.
  `scripts.setup` treats it as a warning rather than a stop, so it still
  creates the database and the admin account.
- **The upload form offered one domain's document types to every deployment**,
  including a "Schematic/Drawing" that most corpora do not contain, with no way
  to name what they do. Declared under `document_family.document_types` in the
  field schema and served to the form.
- The admin screen carried a dead constant holding the first deployment's
  internal contract-model codes. Unused, unreferenced, and shipping.
- **Four more copies of one domain's fact-label list**, each silently wrong on
  any other domain: the Explore graph's default label filter, the per-answer
  evidence tree, the retrieval layer's summary and id-key maps, and the
  sensitivity panel's raw-label pairing. All read the active pack now.
- `scripts.view_coverage` reported on one domain's categories by name, so it
  printed a page of zeroes and a heading about a retired mechanism anywhere
  else. It now reports schema coverage by capture mechanism, for whatever
  domain is loaded.
- `scripts.reconcile_kb` carried a hardcoded regression fixture from a private
  corpus, so every other corpus got a permanent "matcher broken" verdict on a
  case it had never contained. Configurable and skipped when unset.
- **The knowledge view and the review heatmap ordered categories against a
  hardcoded list of one domain's category names, and silently dropped every
  category not in it.** On the domain this repository ships that hid 16 of 21
  fields from both screens, with no error anywhere. Both now order by the
  active schema's own declaration order, and a category the schema does not
  declare sorts to the end rather than disappearing.
- **A verifier correcting a field to "Not Stated" superseded the real value
  instead of falling through to it.** That correction is how a person says "the
  model read a value into a document that does not contain one", so treating it
  as a statement inverted its meaning and buried the earlier document's value.
- **`httpx` was a test-only dependency, but three shipped modules import it**:
  the Content Understanding reader and the two scripts that talk to a running
  instance. Recent `openai` releases vendor a renamed copy, so a clean install
  had no importable `httpx` and all three failed at run time.
- `scripts/_session.py` resolved its login by reading the account list off the
  server, which stopped working when that endpoint was correctly gated behind
  admin. It now uses `BOOTSTRAP_ADMIN_EMAIL`.
- A correction needs a second verifier to concur, which on a single-account
  instance meant it could never land. The bar is a setting now, and the
  documentation says to lower it when you are the only operator.
- The schema-first extraction prompt had no rule against reading a field's own
  name back as its value. An amendment saying "the minimum commitment is
  unchanged" was enough to make the model state one.
- The chat's aggregation tool described itself with field names from the domain
  this framework was extracted from, which are fiction anywhere else.
- **`GET /auth/accounts` had no authentication gate**, so any anonymous caller
  could list every account's email address. Under passwordless sign-in an email
  address is the credential, which made this a full authentication bypass. It
  was a leftover from a demo login picker the UI no longer has. Now admin-only,
  and `tests/api/test_rbac_matrix.py` sweeps the app's own routing table so a
  new endpoint is gated by default rather than by someone remembering.
- `python -m scripts.setup` refused to run in a container, because it required a
  `.env` file when a container is handed its settings by the orchestrator. It
  now checks that the settings are present, not that a particular file exists.
- The README embedded two screenshots the export deliberately leaves behind, so
  a published repository would have opened on two broken images. The
  documentation that ships no longer names the private domain or links to files
  the export omits, and `tests/test_docs.py` now fails the build on either.
- The in-app help guide shipped seven screenshots captured on one deployment,
  carrying its former product name and its counterparties' names into every
  copy. They are gone. The steps carry the instruction on their own, and the
  component still supports images so an instance can add its own.
- The chat cold-start screen suggested six questions naming one domain's
  fields, which read as nonsense against any other schema. They now demonstrate
  kinds of question rather than particular fields.
- Module and configuration READMEs referred to the domain this framework was
  extracted from, and to an account seeder that no longer exists.
- **The documentation still described the retired external reference registers
  as a live pipeline stage**, including a stage table pointing at a deleted
  module and a data-model page listing one deployment's spreadsheet as a second
  required input. The code had been retired correctly, the prose had not, which
  left a reader looking for a feature that is not there.
- **The amendment chain read five field names that only one domain uses.** The
  supersedence walk asked for `relationship_type`, `supplemental_ordinal`,
  `document_date`, `amends_agreement_dated` and `amendment_effective_date`. On
  the domain this repository ships, four of those do not exist and the fifth is
  called something else, so every lookup returned nothing, every document tied
  on an undated sentinel, and "the latest document that sets a field wins"
  quietly became "the last document id alphabetically wins". Which field plays
  which structural role is now declared per domain under
  `document_family.field_roles`, a role naming a field the view does not have
  fails the config build, and `scripts.setup --check` reports the mapping.
- **An admin declaring "this document amends that one" produced no edge.** The
  uploader's declaration is the engine's own vocabulary, but it was being looked
  up in the domain's map of the words its DOCUMENTS use. Those two vocabularies
  coincided in the first domain and nowhere else, so the declared link silently
  vanished.
- **Confidential redaction was inert on the shipped domain.** The single global
  access policy named `Charge` and `Rate`, labels that exist only in the private
  domain, so the class matched nothing and every viewer saw every payment.
  Policy is per domain now, the fallback file deliberately names no labels
  rather than pretending to protect something, and a class naming a label the
  active pack does not declare is reported at setup.
- **The raw source PDF was served to any logged-in account**, regardless of
  clearance, which defeated in one request the redaction every other surface
  performs. Same for two review endpoints that returned raw OCR text with no
  sensitivity filter. All three now require clearance.
- **Clicking Extract after a schema edit re-read nothing and reported success.**
  The field cache keys on the file existing, not on what is in it, so a new
  field came back blank and looked exactly like a field the model had looked for
  and not found. Re-reading is now an explicit option on the endpoint, because
  it spends money.
- **Chat's arithmetic added numbers carrying different units** and labelled the
  total with whichever unit appeared most often, handing the model a confident
  wrong figure. It now returns per-unit subtotals and refuses the single total,
  which is what the knowledge-base surface already did.
- **A failed re-render destroyed the page images** and left a stamp claiming the
  render was complete, so the document could never be recovered through the
  interface. Rendering now writes to a temporary directory and swaps on success.
- **A model response that failed the schema was cached before validation**, so
  every later run replayed it, re-derived the same empty result, and said
  nothing. That page lost all correction and classification permanently.
- **The durable file mirror compared file sizes only**, so a verifier correcting
  a value to a same-length string, or a second vote arriving, produced no
  upload. The correction was then lost on the next container recreation. It
  compares content now.
- **A corrupt extraction file dropped a document from the knowledge base in
  silence**, leaving its old values marked current forever and removing it from
  its family's supersedence walk.
- **The client's name shipped inside the engine**, in the on-disk cache prefix
  and a legacy overlay filename, and the export's own content scanner had been
  told to ignore both. The scanner's exceptions are gone, the drift gate's
  allowlist is empty, and the scan now runs over the tree that actually ships
  rather than the source it was copied from.
- **The shipped chat prompts carried one deployment's world**, including its
  field names, its units, a counterparty from its corpus and a real street
  address. The defaults are domain-neutral now, with per-domain overrides
  resolved the same way the extraction prompt already was.
- **Every screen said "contract"**, which is wrong for a corpus of medical
  records or planning applications. The noun is a setting.
- The Explore graph dropped three edge types the shipped domain declares and
  named five that do not exist, and a fact node with no per-label formatter
  rendered as a raw property name with no amount. Both read the active pack.
- The chat error stream sent raw exception text to the browser. It sends a
  sentence and a log reference now, which is the policy every other route
  already had.
- Uploads, feedback images and thread state were read with no size limit, so
  one authenticated request could exhaust memory.
- `/graph/subgraph` took an unbounded id list, read a vector-bearing row per id,
  then issued one sequential lookup per related item, four times over. It is
  capped, projected, and batched into one wave.
- The review-status helper did whole-corpus file reads on the event loop, which
  is invisible at three documents and a full stall at a thousand.

### Added

- `APP_DOCUMENT_NOUN` and `APP_DOCUMENT_NOUN_PLURAL`.
- `document_family.field_roles` in the field schema, and `setup --check` now
  reports both silent misconfigurations it can detect: an unmapped chain role
  and an access-policy class naming a label the pack does not declare.
- Per-domain chat prompt overrides: `configs/prompts/chat_planner.<domain>.md`,
  `chat_agent.<domain>.md`, `chat_synth.<domain>.md`.
- Per-domain access policy: `configs/policy/sensitivity.<domain>.yaml`.
- `NOTICE`, naming the two dependencies whose licences ask for it.
- `--gold` on the extraction scorer, which otherwise defaults to the active
  domain's directory rather than a hardcoded one.

### Changed

- Rendering moved from PyMuPDF to pypdfium2. PyMuPDF is AGPL, which cannot ship
  inside an MIT-licensed project.
- The first admin account comes from `BOOTSTRAP_ADMIN_EMAIL` instead of a
  spreadsheet, so a fresh install can sign in with no extra files.
- The default Cosmos database name is now `verbatim`.
- The retrieval scoper's stopword list is domain configuration, declared under
  `identity.generic_tokens` in each ontology, rather than one engineering-contract
  list in the engine. A domain that declares none still works.
- Field extractions are written to `storage/fields/`. An install that already
  has the old directory keeps using it, because that cache is presence-based and
  repointing it would force paid re-extraction of the whole corpus.

### Removed

- `pandas` and `xlrd` dependencies, along with the spreadsheet-reading account
  seeder they existed for.

## [0.2.0]

The single-domain product this framework was extracted from: schema-first
extraction, human field verification, per-field supersedence across an
amendment chain, and evidence-anchored chat over a scanned contract corpus.
