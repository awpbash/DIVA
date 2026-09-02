"""FastAPI backend: the API and the built web bundle are served from one process.

Layout::

    api/
      main.py         FastAPI app + lifespan + middleware + router includes
      appdb.py        App-state SQLite: accounts, sessions, chat history, jobs
      deps.py         Shared singletons: async Cosmos store, async OpenAI client
      settings.py     Runtime settings (branding, CORS, retrieval tuning, models)
      rag/            Planner + retriever + synth + citation validator
      routes/         HTTP/SSE endpoints

Run::

    python -m uvicorn api.main:app --reload --port 8000
"""

# The one source of truth for the release version. pyproject.toml carries the
# same string and tests/test_version.py fails the build if the two drift.
__version__ = "0.3.0"
