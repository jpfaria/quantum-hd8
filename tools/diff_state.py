"""Diff two flattened UCNet state dicts (path -> value).

CLI: python3 tools/diff_state.py a.json b.json
"""
from __future__ import annotations

import json
import sys


def diff(a: dict, b: dict) -> list[tuple[str, object, object]]:
    out = []
    for path in sorted(set(a) | set(b)):
        before = a.get(path)
        after = b.get(path)
        if before != after:
            out.append((path, before, after))
    return out


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    a_path, b_path = argv
    a = json.loads(open(a_path).read())
    b = json.loads(open(b_path).read())
    for path, before, after in diff(a, b):
        print(f"{path}: {before} -> {after}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
