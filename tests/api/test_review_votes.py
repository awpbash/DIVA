"""Unit tests for the multi-verifier vote store + consensus (api/review_votes.py)
and the appdb `verifier` column migration. Pure sqlite/JSON in a tmp dir — free,
no app DB or graph touched."""
from __future__ import annotations

import os

# Runtime config must exist BEFORE the first api import (review.py loads
# Config at module level), so the suite runs on a machine with no .env.
os.environ.setdefault("NEO4J_URI", "bolt://localhost:7687")
os.environ.setdefault("NEO4J_USER", "neo4j")
os.environ.setdefault("NEO4J_PASSWORD", "test-password")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-not-real")

# These assert the ONE-approval / TWO-for-a-correction rule, which is now
# configurable. Pin it before the import that reads it, so an operator who has
# set their own bars in the environment does not change what the suite proves.
os.environ["REVIEW_MIN_APPROVALS"] = "1"
os.environ["REVIEW_CORRECTION_APPROVALS"] = "2"

import json
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from api import review_votes as rv
from pipeline.kb import km


@pytest.fixture()
def cfg(tmp_path):
    return SimpleNamespace(storage_root=tmp_path)


def _v(voter: str, decision: str = "approve", value: str | None = None) -> dict:
    return {"voter": voter, "decision": decision, "value": value,
            "comment": None, "created_at": "2026-07-07T00:00:00+00:00"}


# --------------------------------------------------------------------------- #
# consensus() — the edge-case table
# --------------------------------------------------------------------------- #
def test_no_votes_is_untouched():
    c = rv.consensus([])
    assert c == {"verified": False, "disputed": False, "needs_correction": False,
                 "value": None, "pending_value": None,
                 "confidence": None, "verifiers": [], "n_votes": 0}


def test_single_approve_verifies_machine_value():
    c = rv.consensus([_v("a@x.com")])
    assert c["verified"] and not c["disputed"]
    assert c["value"] is None                       # machine value stands
    assert c["confidence"] == 1.0 and c["n_votes"] == 1
    assert c["verifiers"] == ["a@x.com"]


def test_single_approve_below_quorum_is_pending_not_disputed():
    c = rv.consensus([_v("a@x.com")], quorum=2)
    assert not c["verified"] and not c["disputed"]  # pending, awaiting a second
    assert c["confidence"] == 1.0


def test_approve_vs_reject_tie_is_disputed():
    c = rv.consensus([_v("a@x.com"), _v("b@x.com", "reject")])
    assert not c["verified"] and c["disputed"]
    assert c["confidence"] == 0.5 and c["verifiers"] == []


def test_two_competing_amendments_tie_is_disputed():
    c = rv.consensus([_v("a@x.com", value="7°C"), _v("b@x.com", value="8°C")])
    assert not c["verified"] and c["disputed"]


def test_amendment_majority_beats_machine_value():
    c = rv.consensus([_v("a@x.com", value="7 deg C"), _v("b@x.com", value="7 deg C"),
                      _v("c@x.com")])
    assert c["verified"] and c["value"] == "7 deg C"
    assert c["confidence"] == round(2 / 3, 3)
    assert c["verifiers"] == ["a@x.com", "b@x.com"]


def test_lone_correction_is_pending_not_verified():
    # Overriding the machine needs a second pair of eyes: one correction vote
    # surfaces as pending_value, the field stays unverified and undisputed.
    c = rv.consensus([_v("a@x.com", value="8 deg C")])
    assert not c["verified"] and not c["disputed"]
    assert c["value"] is None and c["pending_value"] == "8 deg C"


def test_correction_verifies_at_its_own_bar():
    c = rv.consensus([_v("a@x.com", value="8 deg C"), _v("b@x.com", value="8 deg C")])
    assert c["verified"] and c["value"] == "8 deg C"
    assert c["pending_value"] is None
    assert c["verifiers"] == ["a@x.com", "b@x.com"]


def test_machine_majority_wins_while_correction_stays_open():
    # Two endorse the machine value, one proposes a correction: the machine
    # value verifies (its bar is 1) and the correction remains a visible vote.
    c = rv.consensus([_v("a@x.com"), _v("b@x.com"), _v("c@x.com", value="8 deg C")])
    assert c["verified"] and c["value"] is None
    assert c["pending_value"] is None and not c["disputed"]


def test_reject_majority_is_needs_correction():
    c = rv.consensus([_v("a@x.com", "reject"), _v("b@x.com", "reject"), _v("c@x.com")])
    assert not c["verified"] and c["disputed"] and c["needs_correction"]
    assert c["confidence"] == round(2 / 3, 3) and c["verifiers"] == []


def test_tie_is_disputed_but_not_needs_correction():
    c = rv.consensus([_v("a@x.com", "reject"), _v("b@x.com")])
    assert c["disputed"] and not c["needs_correction"]


def test_majority_approval_over_a_lone_reject():
    c = rv.consensus([_v("a@x.com"), _v("b@x.com"), _v("c@x.com", "reject")])
    assert c["verified"] and not c["disputed"]
    assert c["confidence"] == round(2 / 3, 3)


# --------------------------------------------------------------------------- #
# Vote store — latest-per-voter wins, history retained
# --------------------------------------------------------------------------- #
def test_revote_supersedes_but_keeps_audit_rows(cfg):
    rv.cast_vote("doc1", "a.b", "eng@x.com", "approve", cfg=cfg)
    rv.cast_vote("doc1", "a.b", "eng@x.com", "reject", cfg=cfg)
    cur = rv.current_votes("doc1", "a.b", cfg=cfg)
    assert len(cur) == 1 and cur[0]["decision"] == "reject"
    with rv._conn(cfg) as c:
        assert c.execute("SELECT COUNT(*) FROM field_votes").fetchone()[0] == 2


def test_doc_votes_groups_by_field(cfg):
    rv.cast_vote("doc1", "a.b", "e1@x.com", "approve", cfg=cfg)
    rv.cast_vote("doc1", "c.d", "e1@x.com", "approve", value="42", cfg=cfg)
    rv.cast_vote("doc1", "c.d", "e2@x.com", "approve", value="42", cfg=cfg)
    by_field = rv.doc_votes("doc1", cfg=cfg)
    assert set(by_field) == {"a.b", "c.d"}
    assert len(by_field["c.d"]) == 2


def test_bad_decision_rejected(cfg):
    with pytest.raises(ValueError):
        rv.cast_vote("doc1", "a.b", "e@x.com", "maybe", cfg=cfg)


# --------------------------------------------------------------------------- #
# compile_entry — consensus merges over evidence corrections, never wipes them
# --------------------------------------------------------------------------- #
def test_compile_entry_preserves_evidence_corrections(cfg):
    entry = {"rejected_evidence": ["3:some clause"], "added": [{"value": "x"}]}
    rv.cast_vote("doc1", "a.b", "e1@x.com", "approve", value="7", cfg=cfg)
    out = rv.compile_entry(entry, rv.current_votes("doc1", "a.b", cfg=cfg))
    assert out["rejected_evidence"] == ["3:some clause"]
    assert out["added"] == [{"value": "x"}]
    # One correction vote: pending until a second verifier concurs.
    assert not out["verified"] and out["pending_value"] == "7"
    rv.cast_vote("doc1", "a.b", "e2@x.com", "approve", value="7", cfg=cfg)
    out = rv.compile_entry(entry, rv.current_votes("doc1", "a.b", cfg=cfg))
    assert out["rejected_evidence"] == ["3:some clause"]
    assert out["verified"] and out["value"] == "7"
    assert out["verifier"] == "e2@x.com" and out["at"]
    assert out["verifiers"] == ["e1@x.com", "e2@x.com"] and out["n_votes"] == 2


def test_correction_evidence_rides_the_winning_vote(cfg):
    ev = json.dumps({"snippet": "the corrected clause", "page": 4,
                     "rects": "[{\"page_no\": 4, \"bbox\": [0.1, 0.2, 0.5, 0.25]}]"})
    rv.cast_vote("doc1", "a.b", "e1@x.com", "approve", value="7", evidence=ev, cfg=cfg)
    out = rv.compile_entry({}, rv.current_votes("doc1", "a.b", cfg=cfg))
    assert out["evidence"] is None                     # pending: no anchor yet
    rv.cast_vote("doc1", "a.b", "e2@x.com", "approve", value="7", cfg=cfg)
    out = rv.compile_entry({}, rv.current_votes("doc1", "a.b", cfg=cfg))
    assert out["verified"] and out["evidence"]["page"] == 4
    assert out["evidence"]["snippet"] == "the corrected clause"
    # Winner flips back to the machine value: the stale clause must clear.
    rv.cast_vote("doc1", "a.b", "e1@x.com", "approve", cfg=cfg)
    rv.cast_vote("doc1", "a.b", "e2@x.com", "approve", cfg=cfg)
    rv.cast_vote("doc1", "a.b", "e3@x.com", "approve", cfg=cfg)
    out = rv.compile_entry(out, rv.current_votes("doc1", "a.b", cfg=cfg))
    assert out["verified"] and out["value"] is None and out["evidence"] is None


def test_evidence_column_migrates_on_old_schema(cfg):
    # A pre-evidence database: field_votes WITHOUT the evidence column.
    with sqlite3.connect(cfg.storage_root / "app.db") as c:
        c.execute("CREATE TABLE field_votes ("
                  "id INTEGER PRIMARY KEY AUTOINCREMENT, doc_id TEXT NOT NULL, "
                  "field_key TEXT NOT NULL, voter TEXT NOT NULL, "
                  "decision TEXT NOT NULL, value TEXT, comment TEXT, "
                  "created_at TEXT NOT NULL)")
    rv.cast_vote("doc1", "a.b", "e1@x.com", "approve", value="7",
                 evidence="{\"page\": 2}", cfg=cfg)
    votes = rv.current_votes("doc1", "a.b", cfg=cfg)
    assert votes[0]["evidence"] == "{\"page\": 2}"


def test_compile_entry_unverified_clears_legacy_keys(cfg):
    rv.cast_vote("doc1", "a.b", "e1@x.com", "reject", cfg=cfg)
    out = rv.compile_entry({"verified": True, "verifier": "old@x.com"},
                           rv.current_votes("doc1", "a.b", cfg=cfg))
    assert not out["verified"] and out["disputed"] and out["needs_correction"]
    assert out["verifier"] is None and out["verifiers"] == []


# --------------------------------------------------------------------------- #
# Stale machine-value approvals — an approve-without-value endorses the value
# it SAW (value_seen digest), not whatever a later re-extraction produces.
# --------------------------------------------------------------------------- #
def test_stale_machine_approval_excluded(cfg):
    old = rv.machine_digest(["7 deg C"])
    rv.cast_vote("doc1", "a.b", "e1@x.com", "approve", value_seen=old, cfg=cfg)
    out = rv.compile_entry({}, rv.current_votes("doc1", "a.b", cfg=cfg),
                           machine_digest=old)
    assert out["verified"] and "stale_approvals" not in out
    # Re-extraction changed the value: the old endorsement must not carry over.
    new = rv.machine_digest(["9 deg C"])
    out = rv.compile_entry(out, rv.current_votes("doc1", "a.b", cfg=cfg),
                           machine_digest=new)
    assert not out["verified"] and out["n_votes"] == 0 and out["verifiers"] == []
    assert out["stale_approvals"] is True
    # A fresh approval of the new value re-verifies and clears the flag.
    rv.cast_vote("doc1", "a.b", "e2@x.com", "approve", value_seen=new, cfg=cfg)
    out = rv.compile_entry(out, rv.current_votes("doc1", "a.b", cfg=cfg),
                           machine_digest=new)
    assert out["verified"] and out["verifiers"] == ["e2@x.com"]
    assert "stale_approvals" not in out


def test_legacy_votes_without_snapshot_stay_valid(cfg):
    # Pre-column votes carry NULL value_seen: they must keep their standing
    # (the existing verified fields are not nuked by the migration).
    rv.cast_vote("doc1", "a.b", "e1@x.com", "approve", cfg=cfg)
    out = rv.compile_entry({}, rv.current_votes("doc1", "a.b", cfg=cfg),
                           machine_digest=rv.machine_digest(["anything new"]))
    assert out["verified"] and out["verifiers"] == ["e1@x.com"]


def test_corrections_ignore_the_machine_digest(cfg):
    # A correction endorses its own explicit value, never "the machine value",
    # so the digest check does not apply to it.
    rv.cast_vote("doc1", "a.b", "e1@x.com", "approve", value="8 deg C", cfg=cfg)
    rv.cast_vote("doc1", "a.b", "e2@x.com", "approve", value="8 deg C", cfg=cfg)
    out = rv.compile_entry({}, rv.current_votes("doc1", "a.b", cfg=cfg),
                           machine_digest=rv.machine_digest(["7 deg C"]))
    assert out["verified"] and out["value"] == "8 deg C"


def test_no_digest_skips_the_stale_check(cfg):
    rv.cast_vote("doc1", "a.b", "e1@x.com", "approve",
                 value_seen=rv.machine_digest(["7 deg C"]), cfg=cfg)
    out = rv.compile_entry({}, rv.current_votes("doc1", "a.b", cfg=cfg))
    assert out["verified"]


# --------------------------------------------------------------------------- #
# Legacy overlay migration — once, idempotent
# --------------------------------------------------------------------------- #
def test_migrate_legacy_overlays_once_and_idempotent(cfg, tmp_path):
    review = tmp_path / "review"
    review.mkdir()
    (review / "doc9.verified.json").write_text(json.dumps({
        "a.b": {"verified": True, "value": "S$100", "verifier": "adm@x.com",
                "at": "2026-07-01T00:00:00+00:00"},
        "c.d": {"rejected_evidence": ["1:wrong clause"]},     # corrections-only: untouched
    }), encoding="utf-8")

    assert rv.migrate_legacy_overlays(review, cfg=cfg) == 1
    overlay = json.loads((review / "doc9.verified.json").read_text(encoding="utf-8"))
    entry = overlay["a.b"]
    assert entry["verified"] and entry["value"] == "S$100"
    assert entry["verifiers"] == ["adm@x.com"] and entry["confidence"] == 1.0
    assert entry["at"] == "2026-07-01T00:00:00+00:00"         # original timestamp kept
    assert "verifiers" not in overlay["c.d"]                  # nothing to migrate
    votes = rv.current_votes("doc9", "a.b", cfg=cfg)
    assert len(votes) == 1 and votes[0]["voter"] == "adm@x.com"

    # Second run: guard key present → no new votes, no rewrite churn.
    assert rv.migrate_legacy_overlays(review, cfg=cfg) == 0
    assert len(rv.current_votes("doc9", "a.b", cfg=cfg)) == 1


def test_migrate_missing_dir_is_noop(cfg, tmp_path):
    assert rv.migrate_legacy_overlays(tmp_path / "nope", cfg=cfg) == 0


# --------------------------------------------------------------------------- #
# appdb users.verifier — column migration on an old-schema DB
# --------------------------------------------------------------------------- #
def test_appdb_verifier_column_migration(tmp_path, monkeypatch):
    from api import appdb
    db = tmp_path / "app.db"
    # A pre-flag database: users table WITHOUT the verifier column.
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE users (email TEXT PRIMARY KEY, name TEXT, "
                  "title TEXT, role TEXT NOT NULL, zone TEXT)")
        c.execute("INSERT INTO users VALUES('eng@x.com','Eng','','default','All')")
    monkeypatch.setattr(appdb, "_db_path", lambda: db)

    appdb.init()                                       # migrates + is re-runnable
    appdb.init()
    user = appdb.get_user("eng@x.com")
    assert user["verifier"] == 0                       # default: not a verifier

    appdb.update_account("eng@x.com", verifier=True)
    assert appdb.get_user("eng@x.com")["verifier"] == 1
    # A partial edit that doesn't mention the flag keeps it.
    appdb.update_account("eng@x.com", name="Engineer")
    assert appdb.get_user("eng@x.com")["verifier"] == 1
    appdb.update_account("eng@x.com", verifier=False)
    assert appdb.get_user("eng@x.com")["verifier"] == 0


# --------------------------------------------------------------------------- #
# Review routes — vote endpoint + docs list against a tmp review dir. The
# store/KM/filestore side effects are stubbed (no live graph, no blob push).
# --------------------------------------------------------------------------- #
@pytest.fixture()
def review_rig(tmp_path, monkeypatch):
    monkeypatch.setattr(rv, "_db_path", lambda cfg=None: tmp_path / "app.db")
    from api.routes import review
    from pipeline import filestore
    monkeypatch.setattr(review, "_REVIEW", tmp_path / "review")
    (tmp_path / "review").mkdir()
    monkeypatch.setattr(review, "_recompute_km_field", lambda *a, **k: None)
    monkeypatch.setattr(review, "_propagate_to_km", lambda *a, **k: None)
    monkeypatch.setattr(filestore, "push_async", lambda *a, **k: None)
    return review


_ADMIN = {"email": "adm@x.com", "role": "admin"}


def _write_record(review, doc_id: str, value: str) -> None:
    rec = {"doc_id": doc_id, "title": doc_id, "status_counts": {},
           "fields": {"a.b": {"title": "A", "category": "a", "type": "value",
                              "sensitivity": "general", "status": "green",
                              "values": [{"value": value, "n_mentions": 1,
                                          "evidence": []}]}}}
    (review._REVIEW / f"{doc_id}.json").write_text(
        json.dumps(rec), encoding="utf-8")


def test_blank_correction_is_400_not_a_machine_approval(review_rig):
    review = review_rig
    _write_record(review, "docblank", "7 deg C")
    with pytest.raises(HTTPException) as ei:
        review.vote_field("docblank", "a.b",
                          review.VoteBody(decision="approve", value="   "),
                          user=dict(_ADMIN))
    assert ei.value.status_code == 400
    assert rv.current_votes("docblank", "a.b") == []      # nothing recorded


def test_vote_endpoint_snapshots_and_voids_stale_approvals(review_rig):
    review = review_rig
    _write_record(review, "docstale", "7 deg C")
    review.vote_field("docstale", "a.b", review.VoteBody(),
                      user={"email": "a@x.com", "role": "admin"})
    votes = rv.current_votes("docstale", "a.b")
    assert votes[0]["value_seen"] == rv.machine_digest(["7 deg C"])
    ovp = review._REVIEW / "docstale.verified.json"
    entry = json.loads(ovp.read_text(encoding="utf-8"))["a.b"]
    assert entry["verified"] and entry["verifiers"] == ["a@x.com"]
    # Re-extraction rewrites the record with a different value: the next
    # recompile drops the pre-change endorsement, only the fresh one counts.
    _write_record(review, "docstale", "9 deg C")
    review.vote_field("docstale", "a.b", review.VoteBody(),
                      user={"email": "b@x.com", "role": "admin"})
    entry = json.loads(ovp.read_text(encoding="utf-8"))["a.b"]
    assert entry["verified"] and entry["verifiers"] == ["b@x.com"]
    assert entry["n_votes"] == 1


def test_docs_list_ignores_orphan_overlay_keys(review_rig):
    review = review_rig
    _write_record(review, "docorph", "7 deg C")
    (review._REVIEW / "docorph.verified.json").write_text(json.dumps({
        "a.b": {"verified": True},
        "old.gone": {"verified": True, "needs_correction": True},
    }), encoding="utf-8")
    out = review.list_review_docs(user=dict(_ADMIN))
    d = next(x for x in out["docs"] if x["doc_id"] == "docorph")
    assert d["n_verified"] == 1
    assert d["n_needs_correction"] == 0


# --------------------------------------------------------------------------- #
# reject-evidence v2 — rect + value scoped rejections (km.ev_key2), with the
# legacy page+text key grandfathered field-wide.
# --------------------------------------------------------------------------- #
_R1 = json.dumps([{"page_no": 3, "bbox": [0.1, 0.20, 0.9, 0.24]}])
_R2 = json.dumps([{"page_no": 3, "bbox": [0.1, 0.30, 0.9, 0.34]}])
_SNIP = "Cooling capacity 500 RT"


def _write_record_values(review, doc_id: str, values: list[dict]) -> None:
    rec = {"doc_id": doc_id, "title": doc_id, "status_counts": {},
           "fields": {"a.b": {"title": "A", "category": "a", "type": "value",
                              "sensitivity": "general", "status": "green",
                              "values": values}}}
    (review._REVIEW / f"{doc_id}.json").write_text(
        json.dumps(rec), encoding="utf-8")


def _twin_row_values() -> list[dict]:
    # Two values citing the SAME text on the SAME page (near-identical table
    # rows) — only the rects tell the clauses apart.
    return [
        {"value": "500 RT", "n_mentions": 2, "evidence": [
            {"snippet": _SNIP, "page": 3, "rects": _R1, "kind": "table"},
            {"snippet": _SNIP, "page": 3, "rects": _R2, "kind": "table"}]},
        {"value": "800 RT", "n_mentions": 1, "evidence": [
            {"snippet": _SNIP, "page": 3, "rects": _R1, "kind": "table"}]},
    ]


def test_v2_rejection_scopes_to_rect_twin_and_value(review_rig):
    review = review_rig
    _write_record_values(review, "doctwin", _twin_row_values())
    review.reject_evidence("doctwin", "a.b",
                           review.RejectEvidenceBody(page=3, snippet=_SNIP,
                                                     rects=_R1, value="500 RT"),
                           user=dict(_ADMIN))
    ov = json.loads((review._REVIEW / "doctwin.verified.json")
                    .read_text(encoding="utf-8"))["a.b"]
    assert ov["rejected_evidence"] == [km.ev_key2(3, _SNIP, _R1, "500 RT")]
    eff = review._apply_evidence_overlay(
        review._record_field("doctwin", "a.b"), ov)
    by_val = {v["value"]: v for v in eff["values"]}
    # Only the clicked rect-twin left the clicked value...
    assert [e["rects"] for e in by_val["500 RT"]["evidence"]] == [_R2]
    # ...and the identical text+page+rects on the sibling value survives.
    assert [e["rects"] for e in by_val["800 RT"]["evidence"]] == [_R1]


def test_legacy_rejection_still_strips_field_wide(review_rig):
    review = review_rig
    _write_record_values(review, "docleg", _twin_row_values())
    review.reject_evidence("docleg", "a.b",
                           review.RejectEvidenceBody(page=3, snippet=_SNIP),
                           user=dict(_ADMIN))
    ov = json.loads((review._REVIEW / "docleg.verified.json")
                    .read_text(encoding="utf-8"))["a.b"]
    assert ov["rejected_evidence"] == [km.ev_key(3, _SNIP)]
    eff = review._apply_evidence_overlay(
        review._record_field("docleg", "a.b"), ov)
    # The legacy key matches page+text alone: every citation on both values
    # goes, and with them the values themselves.
    assert eff["values"] == []


# --------------------------------------------------------------------------- #
# Live displaced_values — a winning correction stamps the machine values it
# displaced onto the OpsField (parity with km._build_doc_record), and a
# flip-back to the machine value clears the stamp.
# --------------------------------------------------------------------------- #
def test_vote_correction_stamps_displaced_values(tmp_path, monkeypatch):
    monkeypatch.setattr(rv, "_db_path", lambda cfg=None: tmp_path / "app.db")
    from api.routes import review
    from pipeline import filestore
    monkeypatch.setattr(review, "_REVIEW", tmp_path / "review")
    (tmp_path / "review").mkdir()
    monkeypatch.setattr(review, "_recompute_km_field", lambda *a, **k: None)
    monkeypatch.setattr(filestore, "push_async", lambda *a, **k: None)
    upserts: list[dict] = []

    class FakeStore:
        def point(self, ops_id, doc_id):
            return {"id": ops_id, "pk": doc_id, "kind": "opsfield"}

        def upsert(self, item):
            upserts.append(item)

    monkeypatch.setattr(review, "_store", lambda cfg: FakeStore())
    _write_record_values(review, "docdisp", [
        {"value": "7 deg C", "n_mentions": 1, "evidence": []},
        {"value": "8 deg C", "n_mentions": 1, "evidence": []},
    ])
    # Two verifiers concur on the correction: it wins at its own bar.
    review.vote_field("docdisp", "a.b", review.VoteBody(value="9 deg C"),
                      user={"email": "a@x.com", "role": "admin"})
    review.vote_field("docdisp", "a.b", review.VoteBody(value="9 deg C"),
                      user={"email": "b@x.com", "role": "admin"})
    assert upserts, "the vote path never reached the store"
    assert upserts[-1]["value"] == "9 deg C"
    assert upserts[-1]["displaced_values"] == ["7 deg C", "8 deg C"]
    # Everyone flips back to the machine value: the stamp must clear.
    for email in ("a@x.com", "b@x.com", "c@x.com"):
        review.vote_field("docdisp", "a.b", review.VoteBody(),
                          user={"email": email, "role": "admin"})
    assert upserts[-1]["verified"] is True
    assert upserts[-1]["displaced_values"] == []


# --------------------------------------------------------------------------- #
# Vote thresholds are configuration, not constants.
# --------------------------------------------------------------------------- #
def test_quorum_reads_the_environment(monkeypatch):
    monkeypatch.setenv("REVIEW_TEST_QUORUM", "3")
    assert rv._quorum("REVIEW_TEST_QUORUM", 2) == 3


def test_quorum_falls_back_and_never_drops_below_one(monkeypatch):
    monkeypatch.delenv("REVIEW_TEST_QUORUM", raising=False)
    assert rv._quorum("REVIEW_TEST_QUORUM", 2) == 2
    monkeypatch.setenv("REVIEW_TEST_QUORUM", "")
    assert rv._quorum("REVIEW_TEST_QUORUM", 2) == 2
    monkeypatch.setenv("REVIEW_TEST_QUORUM", "not a number")
    assert rv._quorum("REVIEW_TEST_QUORUM", 2) == 2
    # A bar of zero would verify a field nobody looked at.
    monkeypatch.setenv("REVIEW_TEST_QUORUM", "0")
    assert rv._quorum("REVIEW_TEST_QUORUM", 2) == 1
    monkeypatch.setenv("REVIEW_TEST_QUORUM", "-5")
    assert rv._quorum("REVIEW_TEST_QUORUM", 2) == 1


def test_a_single_operator_can_finish_a_correction():
    """With two verifiers a correction needs a second pair of eyes. With one
    account on the instance that second pair does not exist, so the documented
    "reject it and correct it" step is unreachable unless the bar is settable.
    """
    votes = [{"voter": "a@x.com", "decision": "approve", "value": "Not Stated"}]
    solo = rv.consensus(votes, quorum=1, correction_quorum=1)
    assert solo["verified"] is True
    assert solo["value"] == "Not Stated"
    pair = rv.consensus(votes, quorum=1, correction_quorum=2)
    assert pair["verified"] is False
    assert pair["pending_value"] == "Not Stated"
