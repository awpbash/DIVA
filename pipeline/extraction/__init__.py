"""
The vision-first, whole-doc multi-pass extraction pipeline (v2).

Reads the declarative config tree at ``configs/`` (analyzers, packs,
ontologies, views, prompts) and runs pipeline stages on top of those configs.

Public surface (config loader):

    from pipeline.extraction import load_all, get_analyzer, ConfigError

Pipeline stages live alongside the loader:
    render.py          PDF -> per-page PNG
    rapidocr_ocr.py    PNG -> OCR line detections (local, free)
    correct_classify.py OCR -> markdown + structural blocks (one call per page)
    cu_read.py         the paid alternative reader (layout + tables native)
    merge.py           per-page JSONs -> one ordered doc JSON
    table_repair.py    reconstruct tables the reader mangled
    canonical_lite.py  the structure-only canonical artifact
"""
from .loader import (
    ComposedAnalyzer,
    ConfigError,
    PipelineDefaults,
    get_analyzer,
    load_all,
    render_prompt,
)

__all__ = [
    "ComposedAnalyzer",
    "ConfigError",
    "PipelineDefaults",
    "get_analyzer",
    "load_all",
    "render_prompt",
]
