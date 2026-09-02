"""api/review_votes.py — multi-verifier vote store + consensus (GitHub-style review).

Verification used to be one person's overwrite. It is now a VOTE: every approved
verifier casts approve / reject on a field (approve may carry an amended value),
and the field's state is the CONSENSUS of the current votes (latest per voter).

  * Votes live append-only in storage/app.db (sqlite locking makes concurrent
    verifiers safe; a changed vote is a NEW row, so the full history is the audit
    trail — nothing is ever updated or deleted).
  * ``consensus()`` buckets current votes by the value they endorse: approve with
    no value endorses the machine extraction, approve with a value endorses that
    correction (exact string), reject is its own bucket. A field is verified iff
    one value bucket is the strict winner and meets its own bar: MIN_APPROVALS
    for the machine value, CORRECTION_APPROVALS for a proposed correction (a
    correction is a human overriding the machine, so it needs a second pair of
    eyes before it takes effect). A tie marks the field disputed; a winning
    reject bucket marks it needs_correction (the humans say the value is wrong
    and no accepted alternative exists yet). Confidence = winner / total votes.
  * A correction vote may carry its own EVIDENCE (snippet + page + rects JSON,
    the clause backing the corrected value). When that correction wins, its
    clause becomes the field's displayed evidence and the machine's clause is
    demoted from display (never deleted, the record file keeps it for audit).
  * ``compile_entry()`` writes the consensus INTO the per-doc overlay JSON
    (storage/review/<doc>.verified.json) that km.py and trust.py already consume,
    keeping the legacy keys (verified / value / verifier / at) meaningful — the
    KM build, doc trust tiers and every badge downstream stay one code path.

stdlib sqlite3 only (the ontology_store pattern) — importable without new deps.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from pipeline.config import Config

def _quorum(name: str, default: int) -> int:
    """A vote threshold, from the environment. Never below 1: a bar of zero
    would verify a field nobody looked at."""
    try:
        return max(1, int(os.getenv(name, "").strip() or default))
    except ValueError:
        return default


# How many concurring approvals a value needs before the field counts as
# verified (human_validated). Below the quorum the votes + confidence still
# display, the field just stays pending. Raise to 2 to require two engineers.
MIN_APPROVALS = _quorum("REVIEW_MIN_APPROVALS", 1)

# A proposed correction replaces the machine value everywhere, so it needs a
# second verifier to concur before it takes effect. The proposer's own vote is
# the first of the two. Until the bar is met the correction shows as pending.
#
# Set REVIEW_CORRECTION_APPROVALS=1 on a single-operator instance. Otherwise
# nobody can ever finish a correction there, because the second pair of eyes
# does not exist. Two is the right default wherever two people do exist.
CORRECTION_APPROVALS = _quorum("REVIEW_CORRECTION_APPROVALS", 2)

_DDL = """
CREATE TABLE IF NOT EXISTS field_votes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id     TEXT NOT NULL,
    field_key  TEXT NOT NULL,
    voter      TEXT NOT NULL,
    decision   TEXT NOT NULL CHECK (decision IN ('approve','reject')),
    value      TEXT,
    comment    TEXT,
    evidence   TEXT,
    value_seen TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_field_votes_field ON field_votes(doc_id, field_key);
"""


def _db_path(cfg: Config | None = None) -> Path:
    return (cfg or Config.load()).storage_root / "app.db"


def _conn(cfg: Config | None = None) -> sqlite3.Connection:
    c = sqlite3.connect(_db_path(cfg))
    c.row_factory = sqlite3.Row
    c.executescript(_DDL)
    # Databases created before correction evidence / value snapshots existed
    # lack the columns.
    cols = {r["name"] for r in c.execute("PRAGMA table_info(field_votes)")}
    if "evidence" not in cols:
        c.execute("ALTER TABLE field_votes ADD COLUMN evidence TEXT")
    if "value_seen" not in cols:
        c.execute("ALTER TABLE field_votes ADD COLUMN value_seen TEXT")
    return c


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Vote store (append-only)
# --------------------------------------------------------------------------- #
def cast_vote(doc_id: str, field_key: str, voter: str, decision: str,
              value: str | None = None, comment: str | None = None, *,
              evidence: str | None = None, value_seen: str | None = None,
              at: str | None = None, cfg: Config | None = None) -> int:
    """Record one vote. A voter's newer vote supersedes their older one at read
    time (latest row per voter); the old rows remain as the audit history.
    ``evidence`` is a JSON blob {snippet, page, rects} carried by a correction
    vote: the clause the corrector says backs the new value.
    ``value_seen`` is the machine_digest() of the values an approve-without-value
    vote endorsed, so a later re-extraction voids the endorsement instead of
    silently re-verifying a value the voter never saw (NULL = legacy, stays valid).
    ``at`` exists so the legacy-overlay migration can keep original timestamps."""
    if decision not in ("approve", "reject"):
        raise ValueError(f"decision must be approve|reject, got {decision!r}")
    with _conn(cfg) as c:
        cur = c.execute(
            "INSERT INTO field_votes(doc_id, field_key, voter, decision, value, comment, evidence, value_seen, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (doc_id, field_key, voter.strip().lower(), decision,
             value, comment, evidence, value_seen, at or _now()))
        return int(cur.lastrowid)


def current_votes(doc_id: str, field_key: str, cfg: Config | None = None) -> list[dict]:
    """The standing vote of each voter on one field (their latest row)."""
    with _conn(cfg) as c:
        rows = c.execute(
            "SELECT v.* FROM field_votes v JOIN ("
            "  SELECT voter, MAX(id) AS mid FROM field_votes"
            "  WHERE doc_id=? AND field_key=? GROUP BY voter) m ON m.mid = v.id "
            "ORDER BY v.id",
            (doc_id, field_key)).fetchall()
    return [dict(r) for r in rows]


def doc_votes(doc_id: str, cfg: Config | None = None) -> dict[str, list[dict]]:
    """All standing votes of one document, grouped by field — one query, so the
    review-record endpoint doesn't hit sqlite once per field."""
    with _conn(cfg) as c:
        rows = c.execute(
            "SELECT v.* FROM field_votes v JOIN ("
            "  SELECT field_key, voter, MAX(id) AS mid FROM field_votes"
            "  WHERE doc_id=? GROUP BY field_key, voter) m ON m.mid = v.id "
            "ORDER BY v.id",
            (doc_id,)).fetchall()
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["field_key"], []).append(dict(r))
    return out


def recent_votes(limit: int = 200, cfg: Config | None = None) -> list[dict]:
    """Newest vote rows across ALL documents (every cast, including changed
    votes) — the verification half of the admin activity feed."""
    with _conn(cfg) as c:
        rows = c.execute(
            "SELECT * FROM field_votes ORDER BY id DESC LIMIT ?",
            (max(1, min(int(limit), 1000)),)).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# Consensus — pure function over the standing votes
# --------------------------------------------------------------------------- #
def machine_digest(values) -> str:
    """Order-independent fingerprint of a field's machine-extracted values.
    Snapshotted onto approve-without-value votes (``value_seen``) at cast time
    and recomputed from the review record at compile time: a mismatch means the
    extraction changed underneath the endorsement."""
    return "\n".join(sorted(str(v) for v in values))


def _stale(v: dict, machine_digest: str | None) -> bool:
    """An approve-without-value vote endorses the machine value AS OF when it
    was cast. When the record's current digest differs from the snapshot the
    voter saw, the vote must not carry over to the new value. Votes with a
    NULL snapshot predate the column and stay valid."""
    if machine_digest is None or v["decision"] != "approve" or (v.get("value") or None):
        return False
    seen = v.get("value_seen")
    return seen is not None and seen != machine_digest


def consensus(votes: list[dict], *, quorum: int = MIN_APPROVALS,
              correction_quorum: int = CORRECTION_APPROVALS) -> dict:
    """Current votes → the field's consensus state.

    Buckets: approve+value=None → the machine value; approve+value → that
    correction (exact-string bucket); reject → its own bucket. The field is
    verified iff a value bucket is the STRICT winner and meets its own bar:
    ``quorum`` for the machine value, ``correction_quorum`` for a correction
    (overriding the machine needs a second pair of eyes). A tie marks the
    field disputed. A winning reject bucket marks it needs_correction: the
    humans say the value is wrong and nobody has proposed an accepted fix.
    A correction that is the strict winner but still short of its bar is
    surfaced as ``pending_value`` so the UI can ask for a concur.
    ``confidence`` is the largest bucket's share of all votes cast;
    ``verifiers`` are the approvers behind the winning value."""
    if not votes:
        return {"verified": False, "disputed": False, "needs_correction": False,
                "value": None, "pending_value": None,
                "confidence": None, "verifiers": [], "n_votes": 0}
    buckets: dict[tuple, list[dict]] = {}
    for v in votes:
        key = ("reject", None) if v["decision"] == "reject" else ("value", v.get("value") or None)
        buckets.setdefault(key, []).append(v)
    ranked = sorted(buckets.items(), key=lambda kv: -len(kv[1]))
    top_key, top = ranked[0]
    tie = len(ranked) > 1 and len(ranked[1][1]) == len(top)
    bar = quorum if top_key[1] is None else correction_quorum
    win = top_key[0] == "value" and not tie and len(top) >= bar
    pending = (top_key[0] == "value" and top_key[1] is not None
               and not tie and not win)
    return {
        "verified": win,
        "disputed": tie or top_key[0] == "reject",
        "needs_correction": top_key[0] == "reject" and not tie,
        "value": top_key[1] if win else None,
        "pending_value": top_key[1] if pending else None,
        "confidence": round(len(top) / len(votes), 3),
        "verifiers": sorted(v["voter"] for v in top) if win else [],
        "n_votes": len(votes),
    }


def compile_entry(entry: dict | None, votes: list[dict], *,
                  quorum: int = MIN_APPROVALS,
                  correction_quorum: int = CORRECTION_APPROVALS,
                  machine_digest: str | None = None) -> dict:
    """Merge the votes' consensus into an overlay entry, preserving the evidence
    corrections (``rejected_evidence`` / ``added``) untouched. The legacy singular
    keys stay meaningful for km.py / trust.py / old readers: ``verifier`` is the
    last approver in the winning bucket, ``at`` the latest vote's timestamp.
    When the winner is a correction, the latest winning vote that carried its
    own clause supplies ``evidence`` ({snippet, page, rects}) — that clause
    becomes the field's displayed anchor. The key is always (re)set so a stale
    clause never survives a change of winner.
    ``machine_digest`` is the record's CURRENT value digest: approve-without-value
    votes whose snapshot differs are excluded (the value changed under them), and
    when that exclusion costs the field its verified status the entry carries
    ``stale_approvals`` so the UI can ask for a re-approve. None skips the check
    (callers without record access, e.g. the legacy migration)."""
    entry = dict(entry or {})
    live = [v for v in votes if not _stale(v, machine_digest)]
    cons = consensus(live, quorum=quorum, correction_quorum=correction_quorum)
    entry.update(cons)
    if len(live) < len(votes) and not cons["verified"] and consensus(
            votes, quorum=quorum, correction_quorum=correction_quorum)["verified"]:
        entry["stale_approvals"] = True
    else:
        entry.pop("stale_approvals", None)
    winning = set(cons["verifiers"])
    winners = [v for v in live if v["voter"] in winning]
    entry["verifier"] = winners[-1]["voter"] if winners else None
    entry["at"] = max((v["created_at"] for v in live), default=None)
    ev = None
    if cons["verified"] and cons["value"]:
        for v in winners:                      # ordered oldest → newest
            if v.get("evidence"):
                try:
                    parsed = json.loads(v["evidence"])
                    ev = parsed if isinstance(parsed, dict) else None
                except (TypeError, json.JSONDecodeError):
                    pass
    entry["evidence"] = ev
    return entry


# --------------------------------------------------------------------------- #
# Legacy migration — single-verifier overlays become one approve vote each
# --------------------------------------------------------------------------- #
def migrate_legacy_overlays(review_dir: Path | None = None,
                            cfg: Config | None = None) -> int:
    """One-time, idempotent: convert pre-vote overlay entries (a lone
    verified/verifier stamp) into one approve vote, then compile the consensus
    keys back into the file. Compiled entries always carry a ``verifiers`` key —
    that is the idempotency guard, no marker table needed. Returns entries migrated."""
    review_dir = review_dir or (cfg or Config.load()).storage_root / "review"
    if not review_dir.exists():
        return 0
    n = 0
    for p in sorted(review_dir.glob("*.verified.json")):
        try:
            overlay = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        doc_id = p.name[: -len(".verified.json")]
        changed = False
        for fk, entry in overlay.items():
            if not isinstance(entry, dict) or "verifiers" in entry or not entry.get("verified"):
                continue
            if not current_votes(doc_id, fk, cfg=cfg):
                cast_vote(doc_id, fk, entry.get("verifier") or "admin", "approve",
                          value=entry.get("value"), at=entry.get("at"), cfg=cfg)
            # Grandfather clause: these entries were verified under the
            # single-verifier regime, so they compile at the old bar. The
            # CORRECTION_APPROVALS bar applies the next time anyone votes.
            overlay[fk] = compile_entry(entry, current_votes(doc_id, fk, cfg=cfg),
                                        correction_quorum=1)
            changed = True
            n += 1
        if changed:
            p.write_text(json.dumps(overlay, indent=2, ensure_ascii=False),
                         encoding="utf-8")
    return n
