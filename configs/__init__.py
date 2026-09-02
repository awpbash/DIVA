"""
configs/ is a declarative configuration tree (analyzers, prompts, validators)
NOT a Python module you import from elsewhere. This empty ``__init__.py`` only
exists so the validators in ``configs/validators/*.py`` can ``from . import
ValidationResult`` cleanly under Python 3.14+ (which deprecated the manual
``__package__`` hacks the loader used to do).

Treat ``configs/`` as data + a handful of validator helpers. All extraction
behavior is driven by the YAML/Markdown files under this tree, loaded by
``pipeline.extraction``.
"""
