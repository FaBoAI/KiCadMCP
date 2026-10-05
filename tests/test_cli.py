import json

from kicad_mcp.cli import main


def test_cli_version(capsys):
    import pytest
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == "0.1.0"


def test_missing_raw_is_json_error(tmp_path, capsys):
    assert main(["raw-summary", str(tmp_path / "missing.raw")]) == 2
    streams = capsys.readouterr()
    assert not streams.out
    assert json.loads(streams.err)["type"] == "FileNotFoundError"


def test_failed_check_returns_nonzero(monkeypatch, capsys):
    from kicad_mcp import kicad
    monkeypatch.setattr(kicad, "run_drc", lambda *a, **kw: {"passed": False, "unconnected": 1})
    assert main(["drc", "board.kicad_pcb", "--output", "artifacts/drc"]) == 1
    assert json.loads(capsys.readouterr().out)["unconnected"] == 1


def test_spice_unverified_completion_is_nonzero(monkeypatch, capsys):
    from kicad_mcp import spice
    monkeypatch.setattr(spice, "run_spice", lambda *a, **kw: {"completed": False, "numerical_complete": None})
    assert main(["spice-run", "test.cir", "--output", "artifacts/spice"]) == 1
    assert json.loads(capsys.readouterr().out)["numerical_complete"] is None


def test_cli_rejects_infinite_timeout(capsys):
    import pytest
    with pytest.raises(SystemExit) as exc:
        main(["drc", "board.kicad_pcb", "--output", "unused", "--timeout", "inf"])
    assert exc.value.code == 2
    assert "positive and finite" in capsys.readouterr().err
