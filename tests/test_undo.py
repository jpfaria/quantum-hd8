import json

from quantum_hd8 import undo


def test_record_then_pop_returns_path_and_before(tmp_path):
    journal = tmp_path / "undo.jsonl"
    undo.record("line/ch1/preampgain", 0.26, 0.5, journal=journal)

    assert undo.pop(journal=journal) == ("line/ch1/preampgain", 0.26)


def test_pop_is_lifo(tmp_path):
    journal = tmp_path / "undo.jsonl"
    undo.record("a", 1, 2, journal=journal)
    undo.record("b", 3, 4, journal=journal)

    assert undo.pop(journal=journal) == ("b", 3)
    assert undo.pop(journal=journal) == ("a", 1)


def test_pop_empty_journal_returns_none(tmp_path):
    journal = tmp_path / "undo.jsonl"
    journal.touch()

    assert undo.pop(journal=journal) is None


def test_pop_missing_file_returns_none(tmp_path):
    journal = tmp_path / "does-not-exist.jsonl"

    assert undo.pop(journal=journal) is None


def test_record_creates_parent_directory(tmp_path):
    journal = tmp_path / "nested" / "undo.jsonl"

    undo.record("a", 1, 2, journal=journal)

    assert journal.exists()


def test_record_appends_json_lines(tmp_path):
    journal = tmp_path / "undo.jsonl"
    undo.record("a", 1, 2, journal=journal)
    undo.record("b", 3, 4, journal=journal)

    lines = journal.read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0]) == {"path": "a", "before": 1, "after": 2}
    assert json.loads(lines[1]) == {"path": "b", "before": 3, "after": 4}


def test_pop_removes_only_the_last_line(tmp_path):
    journal = tmp_path / "undo.jsonl"
    undo.record("a", 1, 2, journal=journal)
    undo.record("b", 3, 4, journal=journal)

    undo.pop(journal=journal)

    lines = journal.read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["path"] == "a"


def test_peek_returns_last_without_removing(tmp_path):
    journal = tmp_path / "undo.jsonl"
    undo.record("a", 1, 2, journal=journal)
    undo.record("b", 3, 4, journal=journal)

    assert undo.peek(journal=journal) == ("b", 3)
    assert undo.peek(journal=journal) == ("b", 3)
    undo.drop_last(journal=journal)
    assert undo.peek(journal=journal) == ("a", 1)


def test_peek_missing_or_empty_returns_none(tmp_path):
    assert undo.peek(journal=tmp_path / "nope.jsonl") is None
