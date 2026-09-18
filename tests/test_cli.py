from quantum_hd8.cli import main


def test_version(capsys):
    assert main(["--version"]) == 0
    assert "quantum-hd8" in capsys.readouterr().out
