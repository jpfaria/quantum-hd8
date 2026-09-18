import json

from tools.diff_state import diff, main


def test_diff():
    assert diff({"a": 1, "b": 2}, {"a": 1, "b": 3, "c": 4}) == [("b", 2, 3), ("c", None, 4)]


def test_main_prints_one_line_per_change_and_returns_zero(tmp_path, capsys):
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    a.write_text(json.dumps({"a": 1, "b": 2}))
    b.write_text(json.dumps({"a": 1, "b": 3, "c": 4}))

    rc = main([str(a), str(b)])

    out = capsys.readouterr().out
    assert rc == 0
    assert out == "b: 2 -> 3\nc: None -> 4\n"
