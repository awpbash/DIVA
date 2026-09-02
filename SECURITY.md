# Security

## Reporting a vulnerability

Please do not open a public issue. Use GitHub's private vulnerability reporting
on this repository ("Security" tab, "Report a vulnerability"). Include what you
found, how to reproduce it, and what an attacker gets out of it.

Expect an acknowledgement within a week. This is a small project maintained
alongside other work, so please be patient with the fix timeline, and let us
agree on a disclosure date together.

## What this software does and does not protect

Read this before putting an instance anywhere other than your own machine.

**Authentication is demo-grade and deliberately so.** Sign-in is passwordless:
typing an email address that has an account signs you in as that account. There
is no password, no second factor, and no identity provider. Anyone who can reach
the app and knows or guesses an account's email address has that account's
access. **Do not expose an instance to the public internet as shipped.** Put it
behind your own authentication, or keep it on a private network.

The pieces that a real deployment would replace are contained: `api/routes/auth.py`
mints the session, `api/appdb.py` stores accounts and sessions. The access
*rules* live elsewhere and do not change when you swap the login.

**Access control is enforced on the server.** Roles (admin, confidential,
default) and the per-field sensitivity policy in `configs/policy/` are applied
before data leaves the API, not by hiding elements in the browser. A user without
clearance never receives the confidential value. This part is real, and a bug in
it is a genuine vulnerability worth reporting.

**Documents leave your machine.** Extraction and chat send document text and page
images to whichever model endpoint you configure. If your documents cannot go to
a third party, point `OPENAI_BASE_URL` at an endpoint you control.

**Secrets live in `.env`**, which is gitignored. `.env.example` ships with
`COSMOS_KEY` empty. When it is unset the code falls back to Microsoft's
published emulator key, which is public by design and works only against a
local emulator. A real deployment sets its own.

**`CHAT_API_KEY`** puts a shared-secret gate in front of every route. It is a
blunt instrument for keeping an internal deployment off the open internet, not a
substitute for per-user authentication.

## Supported versions

The latest release on the default branch. There are no backported security fixes
for older versions.
