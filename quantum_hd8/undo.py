"""LIFO undo journal for writes made by Client.set (see client.py).

One JSON object per line: {"path", "before", "after"}. peek() returns the
last line's (path, before) without removing it; the caller re-sends
`before` as the raw normalized value and only calls drop_last() once the
write is confirmed, so a failed undo keeps its entry. pop() = peek() +
drop_last(). Tests always pass an explicit
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


def _lines(journal: Path) -> list[str]:
    journal = Path(journal)
    if not journal.exists():
        return []
    return journal.read_text().splitlines()


def peek(journal: Path = DEFAULT_JOURNAL) -> tuple[str, object] | None:
    lines = _lines(journal)
    if not lines:
        return None
    entry = json.loads(lines[-1])
    return entry["path"], entry["before"]


def drop_last(journal: Path = DEFAULT_JOURNAL) -> None:
    lines = _lines(journal)
    if not lines:
        return
    Path(journal).write_text("".join(line + "\n" for line in lines[:-1]))


def pop(journal: Path = DEFAULT_JOURNAL) -> tuple[str, object] | None:
    # Rewrites the whole file, no file locking: fine for this CLI's single
    # local user running one command at a time (incl. `scene load
    # --keep-gains`, which calls record() several times in a row via
    # Client.set) -- would race under concurrent writers, out of scope here.
    entry = peek(journal)
    if entry is not None:
        drop_last(journal)
    return entry
