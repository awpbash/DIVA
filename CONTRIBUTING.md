# Contributing

Thanks for looking. This is a small project with a strong opinion about what it
is for, so the most useful thing you can do before writing code is open an issue
and say what you are trying to achieve.

## Getting set up

```bash
git clone https://github.com/awpbash/verbatim.git
cd verbatim
python -m venv .venv && . .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env                            # then put your model API key in it
docker compose up -d cosmos
python -m scripts.setup
```

`python -m scripts.setup --check` re-runs the same checks without changing
anything, which is the fastest way to find out why an instance will not start.

## Before you open a pull request

Three commands, all free, none of which call a model:

```bash
python -m pytest tests/          # the deterministic suite
python -m ruff check .           # lint
cd web && pnpm run build && pnpm test   # typecheck, bundle, and frontend tests
```

CI runs exactly these. A pull request that fails any of them will not be
reviewed until it passes, not out of ceremony but because every one of them has
caught a real defect.

## The rules that are not negotiable

These come from what the system promises its users. A change that breaks one of
them is a defect regardless of how well it works otherwise.

1. **Evidence must point at the right place.** Every extracted value carries a
   verbatim snippet and a bounding box on the source page. A human verifies
   values by looking at the highlight. If the highlight is wrong, verification is
   worthless, so anything touching rendering, OCR geometry, or evidence anchoring
   needs to be checked against a real document, not just a passing test.

2. **No answer beyond the evidence.** "Not in the documents" and "Not Stated" are
   correct answers. A plausible guess is a defect.

3. **No per-document special cases.** If a fix only works for one file, it is not
   a fix. Solutions belong in the schema and the config, where they apply to
   every document that follows.

4. **One domain per deployment.** The active domain resolves once, at import,
   from `VERBATIM_DOMAIN` or `configs/pipeline.yaml`. Do not write a domain name
   as a string literal in `pipeline/`, `api/`, or `scripts/`.
   `tests/extraction/test_active_domain.py` fails the build if you do.

## Style

Match the code around you. Comment density, naming, and structure vary by module
and that is deliberate. Lint enforces correctness rules only: undefined names,
unused imports, mutable default arguments, exceptions that lose their cause.
There is no formatter, no import sorter, and no line-reflow bot, because a diff
that touches 400 files to move quotes around destroys the history that explains
why the code is the way it is.

Two specific asks:

- Comments should say why, not what. The code already says what.
- No em-dashes and no semicolons in prose. Use separate sentences, commas, or
  colons.

## Documentation

Documentation is part of the change, not a follow-up. If you alter behaviour a
page describes, alter the page in the same pull request.

`tests/test_docs.py` walks every link and image from `README.md` and
`docs/README.md` and fails on anything that does not resolve, so a moved file
or a renamed anchor is caught before review rather than after publishing.

The figures are generated, not drawn. Source and regeneration steps are in
[`docs/diagrams/README.md`](docs/diagrams/README.md). Each figure makes exactly
one claim, stated in a comment above the function that draws it. If your change
makes that claim untrue, redraw the figure rather than adding a caveat under it.

The sample corpus in [`examples/`](examples/README.md) is a fixture. Every
expected answer in `examples/README.md` is a claim about the wording in
`examples/make_corpus.py`. Change one, change both, and regenerate the PDFs.

## Adding a domain

The engine is config-driven and a new domain is four YAML files, no Python:

| File | What it declares |
| --- | --- |
| `configs/analyzers/<domain>/analyzer.yaml` | Document categories and the party-role taxonomy |
| `configs/ontology/<domain>.yaml` | Node labels, edges, and the party-role vocabulary |
| `configs/packs/<domain>.yaml` | The graph-build contract and the closed ontology |
| `configs/views/<domain>_ops.yaml` | The field schema: what to capture, per field |

`configs/packs/commercial_agreement.yaml` and its siblings are a worked second
domain, deliberately unlike the first one: no equipment tier, different party
roles, an inverted amendment vocabulary. Read it before writing your own, then
point `VERBATIM_DOMAIN` at yours and run `python -m scripts.setup --check`.

## Things that spend money

Extraction, chat, and the evaluation harnesses call a language model, and those
calls cost real money.

Free and deterministic: the test suite, the linter, the extraction scorer,
`scripts.rebuild_kb`, `scripts.build_km`, `scripts.reconcile_kb`,
`scripts.view_coverage`, `scripts.accounts`, and `scripts.setup --check`.

Spends money: `pipeline.ingest` (reading a document), `scripts.ask` (it posts a
real question to a running instance), and `scripts.setup --check-models`, which
makes one small request on purpose to prove the model endpoint answers.

Keep it that way: a contributor should be able to verify a change without a
funded API key.

## Security

Do not open a public issue for a vulnerability. See [SECURITY.md](SECURITY.md).
