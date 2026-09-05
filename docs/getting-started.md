# Getting started

This walkthrough takes DIVA from a fresh checkout to a cited answer. You will
start the application, load three sample contracts, ask a cross-document
question, and review the evidence behind one extracted field. Allow about
twenty minutes, including the first download and document read.

## 1. What you need

| Requirement | Purpose |
| --- | --- |
| Docker | Runs the application and the local knowledge store |
| A model API key | Connects DIVA to OpenAI, Azure AI Foundry, an OpenAI-compatible gateway, or a model server on your network |

Python 3.11 or newer is required for the scripted setup and for running the
application without Docker. The browser setup path only uses the commands in
the next section.

## 2. Install and start

```bash
git clone https://github.com/awpbash/diva.git
cd diva
cp .env.example .env
docker compose up -d
```

The compose stack starts the application and the local Cosmos DB emulator.
First boot can take about a minute while the database becomes healthy.

## 3. Complete the setup wizard

Open <http://localhost:8000>. The wizard creates the initial configuration and
guides you through four choices:

1. **Name the instance**, which sets the label shown in the browser.
2. **Create an administrator account** by entering the email address that will
   be used to sign in.
3. **Add a model API key**, which DIVA tests with a small request before setup
   continues.
4. **Choose a starting path.** **Demo** loads the sample contracts below.
   **Ready-made, as-is** uses the shipped `commercial_agreement` schema.
   **Clone and edit** starts with that schema and opens the field editor.
   **Build from scratch** drafts a field list from a description of your
   document domain, which you can refine before continuing.

When setup finishes, DIVA restarts once and signs you in as the administrator
you created.

> Sign-in is passwordless in the default application setup. That is convenient
> for local evaluation, but an instance shared with other people should be
> placed behind an authentication layer or kept on a private network. See
> [SECURITY.md](../SECURITY.md) and [Deployment](DEPLOYMENT.md) before sharing
> an instance.

If you selected **Demo**, continue at [Ask a question](#5-ask-a-question).

## 4. Load the sample contracts

The sample corpus contains a base software licence agreement and two
amendments. It is arranged to demonstrate how DIVA resolves current values
across a document family. The files are in [`examples/corpus/`](../examples/corpus):

1. `01-master-license-agreement-2023.pdf`
2. `02-first-amendment-2024.pdf`
3. `03-second-amendment-2025.pdf`

Open the **Admin** tab and upload the files in this order. Place them in one
folder, select each document type and date, and identify the two amendments as
amending the master agreement. These declarations establish the document
family, while DIVA extracts and verifies the fields within it.

Extraction runs in the background. Each document becomes searchable when its
own processing finishes, so you can continue as soon as the first document is
ready.

## 5. Ask a question

Open the **Chat** tab and ask:

> What is the current annual licence fee?

The answer should be **SGD 61,500**, with a citation to the First Amendment.
The base agreement sets the fee at SGD 48,000, while the Second Amendment is
the newest document and leaves the fee unchanged. DIVA walks backwards through
the amendment chain until it reaches the document that establishes the field.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="diagrams/supersedence.gif">
  <img alt="Two fields resolved separately by walking backwards along a chain of three documents. The licence fee is found in the first amendment, the initial term in the second." src="diagrams/supersedence.gif">
</picture>

Select the citation. The PDF opens on the relevant page with the supporting
paragraph highlighted, allowing you to read the value in its source context.

![The answer with its citation chip clicked, and the first amendment open beside it with the licence-fee clause highlighted in amber.](images/chat-citation.png)

[`examples/README.md`](../examples/README.md) contains additional questions
and expected answers for exploring the sample corpus.

## 6. Review a field

Open the **Review** tab, select the folder, and open a document. Each extracted
field appears alongside its evidence. Selecting a field highlights the
corresponding clause on the page.

![The review screen: the contract page on the left with the licence-fee clause highlighted, the field list on the right with counters reading eleven confident, nine to check, none needing attention, two blank, and approve, reject and correct buttons on the selected field.](images/review-field.png)

Open the **Second Amendment** and inspect the fields that it changes. Fields
that the amendment leaves untouched continue to resolve from earlier documents
in the family. If a value needs correction, choose **Propose correction** and
enter `Not Stated` when the document does not establish that field. The current
value then falls back to the earlier document, while the correction remains in
the record's history.

On a single-operator instance, set `REVIEW_CORRECTION_APPROVALS=1` in `.env`
before proposing a correction. Shared workflows can keep the default approval
count and use the second-verifier review step.

Ask Chat about the field again and the answer will reflect the verified record.
The document was searchable before review, and the verification action is
carried through to downstream retrieval.

## 7. Where to go next

| Next step | Guide |
| --- | --- |
| Understand the design decisions | [Concepts](concepts.md) |
| Define your own document domain | [Domains](domains.md) |
| Run a scripted setup or individual pipeline stages | [Reference](reference.md#commands) |
| Deploy DIVA for a team | [Deployment](DEPLOYMENT.md) |
| Diagnose an instance | [Troubleshooting](troubleshooting.md) |
| Explore a larger collection | [Real-world corpus](../examples/real_world/README.md) |

## Scripted setup

The browser wizard is the quickest way to explore DIVA. For CI, repeatable
deployments, or environments where a browser is not available, use the same
components from the command line:

```bash
pip install -r requirements.txt
cp .env.example .env
```

Set `OPENAI_API_KEY` and any deployment-specific values in `.env`, then run:

```bash
docker compose up -d cosmos
python -m scripts.setup --check
docker compose up -d
```

Run scripts as modules. On Windows, set `PYTHONUTF8=1` before running them.
The full command catalog, including ingestion, knowledge rebuilding, and
evaluation, is in [Reference](reference.md#commands).

To return a local development instance to its initial state, an administrator
can use the reset action in **Admin > Accounts** or run
`python -m scripts.reset_dev`. This clears the local database, knowledge store,
and uploaded files so the setup wizard opens again on the next boot.
