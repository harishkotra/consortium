"""Check the character limits claimed in docs/x-thread.md and docs/linkedin-post.md.

    .venv/bin/python scripts/counts.py            # default limit 300
    .venv/bin/python scripts/counts.py 280 docs/x-thread.md

Every ```text block in the given files is measured, so a post that quietly grew past a platform
limit fails here instead of failing at paste time.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BLOCK = re.compile(r"^```text\s*\n(.*?)^```", re.M | re.S)


def main(argv: list[str]) -> int:
    limit = 300
    files = ["docs/x-thread.md", "docs/linkedin-post.md"]
    if argv and argv[0].isdigit():
        limit, argv = int(argv[0]), argv[1:]
    if argv:
        files = argv

    failed = False
    for name in files:
        path = ROOT / name
        if not path.exists():
            print(f"{name}: missing")
            failed = True
            continue
        blocks = BLOCK.findall(path.read_text(encoding="utf-8"))
        print(f"\n{name} — {len(blocks)} draft block(s), limit {limit}")
        for index, block in enumerate(blocks, 1):
            text = block.strip()
            over = len(text) > limit
            failed |= over
            print(f"  {index:>2}. {len(text):>4} chars  {'OVER by ' + str(len(text) - limit) if over else 'fits'}")
    print("\n" + ("ALL WITHIN LIMIT" if not failed else "ONE OR MORE BLOCKS EXCEED THE LIMIT"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
