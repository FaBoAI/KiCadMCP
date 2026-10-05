from pathlib import Path

import pytest

from kicad_mcp.paths import Workspace


def test_workspace_allows_nested_output(tmp_path):
    w = Workspace(tmp_path)
    assert w.path("new/result.json") == str(tmp_path / "new/result.json")


def test_traversal_and_sibling_prefix_rejected(tmp_path):
    w = Workspace(tmp_path)
    for value in ("../other", str(tmp_path) + "-sibling/data"):
        with pytest.raises(ValueError, match="inside"):
            w.path(value)


def test_symlink_escape_rejected(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (root / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Symlink creation unavailable")
    with pytest.raises(ValueError, match="inside"):
        Workspace(root).path("link/file")


def test_workspace_must_be_existing_directory(tmp_path):
    f = tmp_path / "file"
    f.write_text("x")
    with pytest.raises(ValueError):
        Workspace(f)
    with pytest.raises(FileNotFoundError):
        Workspace(tmp_path / "missing")
