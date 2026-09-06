"""run_unprivileged.py: drop from root to the app's own uid before running
anything else in a live container.

`railway ssh` (and the equivalent "exec into the running container" command
on most other hosts) lands you as root, even on a deployment where the
actual server process long since dropped to uid 1000 in api/boot_seed.py.
That gap is invisible until you write a file: anything a root shell creates
under storage/ is root-owned, and the server, correctly locked down to its
restricted user, can no longer read it back. That is exactly what broke the
admin dashboard once already, a 500 on a review file an SSH session had
touched. See git history around that fix for the full story.

Prefix any one-off command run over SSH with this instead of running it
bare, so the question of whether you happen to be root this time never
comes up:

    railway ssh --service app -- python scripts/run_unprivileged.py \\
        python -m scripts.accounts list

No-op when already unprivileged (local `docker compose exec`, for
instance), so it is always safe to prefix, never just sometimes needed.
"""
from __future__ import annotations

import os
import sys


def main() -> None:
    if not sys.argv[1:]:
        print("usage: python scripts/run_unprivileged.py <command> [args...]",
              file=sys.stderr)
        sys.exit(2)
    if os.geteuid() == 0:
        os.setgid(1000)
        os.setuid(1000)
    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    main()
