"""Filesystem layout for the extraction pipeline.

Single source of truth for where the pipeline writes things on disk. Build
paths through ``Paths(cfg)``; never hardcode ``storage/...`` strings.

Layout under ``cfg.storage_root``::

    raw/<doc_id>.pdf                          input PDF
    pages/<doc_id>/p_NNN.png                  rendered page (render.py)

    # Extraction pipeline — RapidOCR for geometry + verbatim text, LLM for
    # correction + structural classification:
    rapidocr/<doc_id>/p_NNN.json              raw RapidOCR output (cache)
    pages_md_rapidocr/<doc_id>/p_NNN.json     RapidOCR adapted to VisionPage
    correct_classify/<doc_id>/p_NNN.json      raw LLM response (cache)
    pages_md/<doc_id>/p_NNN.json              final per-page blocks (input to merge)

    # Document-level outputs:
    doc/<doc_id>.json                         merge.py: clean view (no coords)
    doc_geometry/<doc_id>.json                merge.py: block_id -> {bbox, polygon}
    outline/<doc_id>.json                     understand.py outline pass
    harvest/<doc_id>.json                     understand.py harvest pass
    normalise/<doc_id>/<cat>.json             normalise.py per-category cache
    canonical/<doc_id>.json                   normalised facts + validate.py overlay
    runs/<doc_id>/understand_report.json      raw->stub->canonical loss report

    # Visualisation:
    viz/<doc_id>/p_NNN.png                    bbox + polygon overlay (viz_ocr.py)

    runs/<doc_id>/<stage>.{log,stamp}         per-stage logs + idempotency stamps
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from .config import Config


# Where schema-first field extractions live under storage/.
#
# This directory was once named after the first deployment's document type. A
# fallback to the old name lived here so a running install would not orphan its
# cache, which matters because the cache is PRESENCE-based: a file that exists
# is returned as-is and never revalidated, so pointing an install at a new empty
# directory silently forces paid re-extraction of the whole corpus.
#
# The fallback is gone. It could only ever fire on one machine, and it carried
# a customer's name into every copy of this repository. An install that still
# has the old directory renames it once:
#
#     mv storage/<old name> storage/fields
FIELDS_DIR = "fields"


def fields_dir(cfg: Config) -> Path:
    """The field-extraction cache directory."""
    return cfg.storage_root / FIELDS_DIR


class Paths:
    def __init__(self, cfg: Config) -> None:
        self.root = cfg.storage_root

    # --- inputs + rendered pages ----------------------------------------

    def raw_pdf(self, doc_id: str) -> Path:
        return self.root / "raw" / f"{doc_id}.pdf"

    def raw_meta_json(self, doc_id: str) -> Path:
        """Sidecar capturing the original filename / source path / family group
        at intake, so the loader can stamp human document metadata without a
        separate folder scan."""
        return self.root / "raw" / f"{doc_id}.meta.json"

    def pages_dir(self, doc_id: str) -> Path:
        return self.root / "pages" / doc_id

    def page_png(self, doc_id: str, page_no: int) -> Path:
        return self.pages_dir(doc_id) / f"p_{page_no:03d}.png"

    # --- Extraction pipeline (RapidOCR -> LLM correct+classify) ---------

    def cu_raw_json(self, doc_id: str) -> Path:
        """Raw Azure Content Understanding analyze result for the WHOLE doc
        (one paid call per document, ever — content-addressed doc ids make
        this cache permanent)."""
        return self.root / "cu_raw" / f"{doc_id}.json"

    def rapidocr_dir(self, doc_id: str) -> Path:
        """Raw RapidOCR per-page output (boxes + txts + scores as JSON)."""
        return self.root / "rapidocr" / doc_id

    def rapidocr_page(self, doc_id: str, page_no: int) -> Path:
        return self.rapidocr_dir(doc_id) / f"p_{page_no:03d}.json"

    def pages_md_rapidocr_dir(self, doc_id: str) -> Path:
        """Per-page VisionBlock JSONs adapted from RapidOCR output (no
        LLM corrections applied yet)."""
        return self.root / "pages_md_rapidocr" / doc_id

    def pages_md_rapidocr_page(self, doc_id: str, page_no: int) -> Path:
        return self.pages_md_rapidocr_dir(doc_id) / f"p_{page_no:03d}.json"

    def correct_classify_dir(self, doc_id: str) -> Path:
        """Per-page raw LLM correct+classify response (cache)."""
        return self.root / "correct_classify" / doc_id

    def correct_classify_page(self, doc_id: str, page_no: int) -> Path:
        return self.correct_classify_dir(doc_id) / f"p_{page_no:03d}.json"

    def pages_md_dir(self, doc_id: str) -> Path:
        """Per-page VisionPageResponse — final per-page output of the
        extraction pipeline. Consumed by merge.py."""
        return self.root / "pages_md" / doc_id

    def pages_md_page(self, doc_id: str, page_no: int) -> Path:
        return self.pages_md_dir(doc_id) / f"p_{page_no:03d}.json"

    # --- merge -> doc + geometry sidecar --------------------------------

    def doc_json(self, doc_id: str) -> Path:
        """Output of merge.py — clean ordered doc with stable block IDs.
        Block geometry lives in the sibling geometry sidecar so the LLM-
        input file stays compact + free of coordinates."""
        return self.root / "doc" / f"{doc_id}.json"

    def doc_geometry_json(self, doc_id: str) -> Path:
        """Sidecar to doc.json: block_id -> {page_no, bbox, polygon}.
        Read by validate.py + load.py for citation geometry."""
        return self.root / "doc_geometry" / f"{doc_id}.json"

    # --- understand + validate ------------------------------------------

    def outline_json(self, doc_id: str) -> Path:
        return self.root / "outline" / f"{doc_id}.json"

    def harvest_json(self, doc_id: str) -> Path:
        return self.root / "harvest" / f"{doc_id}.json"

    def canonical_json(self, doc_id: str) -> Path:
        """Final canonical fact list (categorise + normalise + validate)."""
        return self.root / "canonical" / f"{doc_id}.json"

    def normalise_dir(self, doc_id: str) -> Path:
        """Per-category normalise cache dir (one JSON per category)."""
        return self.root / "normalise" / doc_id

    def quarantine_json(self, doc_id: str) -> Path:
        """Facts the loader's ontology gate refused (unknown category /
        undeclared edge). Sidecar mirror of the :Proposal nodes; reviewed
        and promoted by editing the ontology YAML, then re-loading."""
        return self.root / "quarantine" / f"{doc_id}.json"

    def extraction_drops_json(self, doc_id: str) -> Path:
        """Facts dropped during categorise/normalise — unmapped label,
        category not enabled on the analyzer, empty value, or
        source-reconstruction failure. The recall-loss review surface;
        distinct from the loader's ontology-reject quarantine above, but
        in the same dir so all quarantine lives in one place."""
        return self.root / "quarantine" / f"{doc_id}.extraction_drops.json"

    def relation_proposals_json(self, doc_id: str) -> Path:
        """LLM-proposed semantic relations awaiting review. These are not
        asserted graph edges; load.py mirrors them to :Proposal nodes."""
        return self.root / "quarantine" / f"{doc_id}.relation_proposals.json"

    def relation_propose_dir(self, doc_id: str) -> Path:
        """Per-batch raw LLM relation-proposal response cache."""
        return self.root / "relation_propose" / doc_id

    def relation_propose_cache(self, doc_id: str, batch_id: str) -> Path:
        return self.relation_propose_dir(doc_id) / f"{batch_id}.json"

    def understand_report_json(self, doc_id: str) -> Path:
        """Audit report for understand.py loss accounting."""
        return self.root / "runs" / doc_id / "understand_report.json"

    def token_usage_json(self, doc_id: str) -> Path:
        """Per-stage LLM token accounting for one ingest run."""
        return self.root / "runs" / doc_id / "token_usage.json"

    def tokens_json(self) -> Path:
        """Consolidated per-stage token ledger across every ingested doc
        (keyed by doc_id), merged on each ingest. The at-a-glance cost log."""
        return self.root / "tokens.json"

    def structural_units_json(self, doc_id: str) -> Path:
        """Deterministic clause/definition/table units segmented from doc.json
        (stage 1 of unit-scoped extraction). The unit the per-unit extraction
        pass harvests from."""
        return self.root / "structural_units" / f"{doc_id}.json"

    def unit_extract_dir(self, doc_id: str) -> Path:
        """Per-unit raw LLM extraction response cache (stage 2)."""
        return self.root / "unit_extract" / doc_id

    def unit_extract_cache(self, doc_id: str, unit_id: str) -> Path:
        return self.unit_extract_dir(doc_id) / f"{unit_id}.json"

    def unit_harvest_json(self, doc_id: str) -> Path:
        """Merged raw_facts from the per-unit extraction pass — same shape as
        harvest_json (raw_facts list) so categorise consumes it unchanged,
        plus a `source_unit` on each fact. The unit-scoped replacement for the
        whole-doc harvest output."""
        return self.root / "unit_harvest" / f"{doc_id}.json"

    # --- bookkeeping ----------------------------------------------------

    def run_log(self, doc_id: str, stage: str) -> Path:
        return self.root / "runs" / doc_id / f"{stage}.log"

    def stage_stamp(self, doc_id: str, stage: str) -> Path:
        """Idempotency stamp. Stores the sha256 of the stage's inputs at the
        time it last ran successfully — if today's input hash matches, the
        stage can skip."""
        return self.root / "runs" / doc_id / f"{stage}.stamp"

    def ensure_parent(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        return path


def atomic_write_text(path: Path, text: str) -> None:
    """Write text via a sibling tmp file + os.replace so a kill mid-write can
    never leave a torn file (several caches treat presence as validity)."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, obj, *, indent: int | None = 2) -> None:
    """JSON twin of atomic_write_text (same tmp-then-replace guarantee)."""
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=indent))


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def doc_id_for(path: Path) -> str:
    """Stable doc id = sha256(content)[:16]. Re-ingesting the same bytes hits caches."""
    return file_sha256(path)[:16]
