"""
CLI: print the fully-composed analyzer (post-merge + inheritance).

Usage:
    python -m pipeline.extraction.show <analyzer_id>
    python -m pipeline.extraction.show <domain> --category money

The --category flag filters to just the roles defined for that category.
"""
from __future__ import annotations

import argparse
import sys

from .loader import ConfigError, get_analyzer


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("analyzer_id",
                   help="an analyzer id, or _universal for the shared base")
    p.add_argument("--category", help="Filter to roles for this category")
    args = p.parse_args()

    try:
        a = get_analyzer(args.analyzer_id, refresh=True)
    except ConfigError as e:
        sys.stderr.write(f"[FAIL] {e}\n")
        return 1

    print(f"== {a.id}  v{a.version}  (cache_key={a.cache_key})")
    if a.extends:
        print(f"   extends: {a.extends}")
    print(f"   thresholds: verified>={a.confidence_verified}  tentative>={a.confidence_tentative}")
    print(f"   classify_when: {a.classify_when or '(fallback)'}")
    print(f"\n   categories ({len(a.categories)}):")
    for c in a.categories:
        marker = "  " if c in a.roles_by_category else "  (no roles defined)"
        print(f"     - {c}{marker if c not in a.roles_by_category else ''}")

    if args.category:
        roles = a.roles_for(args.category)
        if not roles:
            print(f"\n   no roles defined for category {args.category!r}")
        else:
            print(f"\n   roles for category {args.category!r} ({len(roles)}):")
            for r in roles:
                print(f"     - {r}")
    else:
        print("\n   roles by category:")
        for cat in a.categories:
            roles = a.roles_for(cat)
            if roles:
                print(f"     {cat}: {', '.join(roles)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
