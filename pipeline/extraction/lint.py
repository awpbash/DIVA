"""
CLI: validate every config in the tree.

Usage:
    python -m pipeline.extraction.lint
"""
from __future__ import annotations

import sys

from .loader import ConfigError, load_all


def main() -> int:
    try:
        defaults, analyzers = load_all(refresh=True)
    except ConfigError as e:
        sys.stderr.write(f"[FAIL] CONFIG ERROR\n{e}\n")
        return 1
    except Exception as e:
        sys.stderr.write(f"[FAIL] UNEXPECTED\n{type(e).__name__}: {e}\n")
        return 2

    print("[OK] configs/ valid")
    print(
        f"  pipeline.yaml: dpi={defaults.render_dpi}, "
        f"vision_concurrency={defaults.vision_concurrency}, "
        f"verified>={defaults.confidence_verified}, "
        f"tentative>={defaults.confidence_tentative}"
    )
    print(f"  {len(analyzers)} analyzer(s):")
    for aid, a in sorted(analyzers.items()):
        n_roles = sum(len(v) for v in a.roles_by_category.values())
        print(
            f"    - {aid:<22} v{a.version:<6} "
            f"categories={len(a.categories):>2}  "
            f"roles={n_roles:>3} (across {len(a.roles_by_category)} cat)  "
            f"key={a.cache_key}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
