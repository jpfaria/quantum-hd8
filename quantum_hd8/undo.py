"""LIFO undo journal for writes made by Client.set (see client.py).

One JSON object per line: {"path", "before", "after"}. pop() removes and
returns the last line's (path, before) -- the caller re-sends `before` as
the raw normalized value to restore it. Tests always pass an explicit
`journal` (tmp_path); the default is only used by the real CLI.
"""
from __future__ import annotations

import json
from pathlib import Path

DEFAULT_JOURNAL = Path.home() / ".quantum-hd8" / "undo.jsonl"


def record(path: str, before: object, after: object, journal: Path = DEFAULT_JOURNAL) -> None:
    journal = Path(journal)
    journal.parent.mkdir(parents=True, exist_ok=True)
    with journal.open("a") as f:
        f.write(json.dumps({"path": path, "before": before, "after": after}) + "\n")


def pop(journal: Path = DEFAULT_JOURNAL) -> tuple[str, object] | None:
    journal = Path(journal)
    if not journal.exists():
        return None
    lines = journal.read_text().splitlines()
    if not lines:
        return None
    last = lines.pop()
    journal.write_text("".join(line + "\n" for line in lines))
    entry = json.loads(last)
    return entry["path"], entry["before"]
