"""Read-only KiCad inspection and native CLI reports / manufacturing exports.

The source CAD is never saved or zone-filled. Generated files require a dedicated
empty output directory. A successful process is not an electrical pass.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any


_MAC_CLI = "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"
_MAC_PYTHON = "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3"


def _validate_timeout(timeout: float) -> None:
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a positive finite number")


def _executable(value: str | os.PathLike[str]) -> str:
    candidate = str(Path(value).expanduser())
    resolved = shutil.which(candidate)
    if resolved:
        return str(Path(resolved).resolve())
    raise FileNotFoundError(f"Executable not found or not executable: {candidate}")


def resolve_kicad_cli(executable: str | os.PathLike[str] | None = None) -> str:
    """Find kicad-cli. Explicit and KICAD_CLI overrides do not silently fall back."""
    override = executable or os.environ.get("KICAD_CLI")
    if override:
        return _executable(override)
    candidates = ["kicad-cli", _MAC_CLI, "/usr/bin/kicad-cli", "/usr/local/bin/kicad-cli"]
    if os.name == "nt":
        root = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "KiCad"
        candidates.extend(str(p) for p in sorted(root.glob("*/bin/kicad-cli.exe"), reverse=True))
    for candidate in candidates:
        try:
            return _executable(candidate)
        except FileNotFoundError:
            pass
    raise FileNotFoundError("kicad-cli is unavailable. Install KiCad or set KICAD_CLI.")


def resolve_pcbnew_python(python_executable: str | os.PathLike[str] | None = None, timeout: float = 10) -> str:
    """Find a Python interpreter that can import KiCad's native pcbnew module."""
    _validate_timeout(timeout)
    override = python_executable or os.environ.get("KICAD_PYTHON")
    candidates = [override] if override else [sys.executable, _MAC_PYTHON, "python3"]
    failures = []
    seen = set()
    for candidate in candidates:
        if not candidate:
            continue
        try:
            resolved = _executable(candidate)
            if resolved in seen:
                continue
            seen.add(resolved)
            result = subprocess.run(
                [resolved, "-c", "import pcbnew"], capture_output=True, text=True,
                timeout=timeout, check=False,
            )
            if result.returncode == 0:
                return resolved
            failures.append(f"{resolved}: cannot import pcbnew")
        except (OSError, subprocess.TimeoutExpired) as exc:
            failures.append(f"{candidate}: {type(exc).__name__}")
    raise RuntimeError("No Python with pcbnew is available. Set KICAD_PYTHON. " + "; ".join(failures))


def _input(path: str | os.PathLike[str], suffix: str) -> Path:
    source = Path(path).expanduser().resolve(strict=True)
    if not source.is_file() or source.suffix.lower() != suffix:
        raise ValueError(f"Expected a {suffix} file: {source}")
    return source


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _prepare_output(output_dir: str | os.PathLike[str]) -> Path:
    raw = Path(output_dir).expanduser()
    if raw.is_symlink():
        raise ValueError("Output directory must not be a symlink")
    output = raw.resolve()
    if output.exists():
        if not output.is_dir() or any(output.iterdir()):
            raise ValueError(f"Output directory must be new or empty: {output}")
    else:
        output.mkdir(parents=True, exist_ok=False)
    # Empty-directory checks alone race when two MCP calls target the same
    # pre-existing directory. O_EXCL via mode "x" grants one operation ownership.
    # Keep the marker after failure as well: partial results must not be reused.
    with (output / ".kicad-mcp-output.lock").open("x", encoding="utf-8") as marker:
        marker.write("Reserved by KiCadMCP; use a new directory for another operation.\n")
    return output


def _run(command: list[str], timeout: float) -> dict[str, Any]:
    _validate_timeout(timeout)
    try:
        process = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Command timed out after {timeout}s: {command[0]}") from exc
    record = {"command": command, "returncode": process.returncode,
              "stdout": process.stdout, "stderr": process.stderr}
    if process.returncode != 0:
        raise RuntimeError(f"Command failed ({process.returncode}): {' '.join(command)}\n"
                           f"{process.stderr[-4000:]}\n{process.stdout[-4000:]}")
    return record


def _source_unchanged(source: Path, original: str) -> None:
    if _sha(source) != original:
        raise RuntimeError(f"Source file changed during operation; results are not certified: {source}")


def _project_rules(source: Path) -> dict[str, Any]:
    """Expose configured ignores/exclusions instead of interpreting zero as a pass."""
    project = source.with_suffix(".kicad_pro")
    result: dict[str, Any] = {"project_file": str(project) if project.exists() else None,
                              "project_sha256": _sha(project) if project.exists() else None}
    if project.exists():
        try:
            data = json.loads(project.read_text(encoding="utf-8"))
            board = data.get("board", {}).get("design_settings", {})
            erc = data.get("erc", {})
            result.update({"drc_rule_severities": board.get("rule_severities", {}),
                           "drc_exclusions": board.get("drc_exclusions", []),
                           "erc_rule_severities": erc.get("rule_severities", {}),
                           "erc_exclusions": erc.get("erc_exclusions", [])})
            result["ignored_checks"] = {
                "drc": sorted(k for k, v in result["drc_rule_severities"].items() if v == "ignore"),
                "erc": sorted(k for k, v in result["erc_rule_severities"].items() if v == "ignore"),
            }
        except (ValueError, TypeError, AttributeError) as exc:
            result["read_error"] = str(exc)
    else:
        result["warning"] = "No matching project file; KiCad defaults may apply."
    custom = source.with_suffix(".kicad_dru")
    result["custom_rules"] = {"path": str(custom), "sha256": _sha(custom)} if custom.exists() else None
    return result


def _base(source: Path, original: str, tool: str, commands: list[dict[str, Any]]) -> dict[str, Any]:
    return {"schema_version": 1, "source": str(source), "source_sha256": original,
            "source_unchanged": True, "created_utc": datetime.now(timezone.utc).isoformat(),
            "tool": tool, "commands": commands, "rules": _project_rules(source),
            "zone_refill_requested": False,
            "limitations": ["These results are software checks, not physical validation.",
                            "Saved copper may contain stale zone fills; no fill or save is performed."]}


def _finish(output: Path, result: dict[str, Any]) -> dict[str, Any]:
    result["files"] = [{"path": str(p), "sha256": _sha(p), "size_bytes": p.stat().st_size}
                       for p in sorted(output.rglob("*")) if p.is_file()]
    manifest = output / "manifest.json"
    result["manifest"] = str(manifest)
    with manifest.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return result


def inspect_board(board_path: str | os.PathLike[str], python_executable: str | os.PathLike[str] | None = None,
                  timeout: float = 60) -> dict[str, Any]:
    """Inspect native board geometry, layers, footprints, pads and net assignments."""
    _validate_timeout(timeout)
    source = _input(board_path, ".kicad_pcb")
    original = _sha(source)
    interpreter = resolve_pcbnew_python(python_executable)
    worker = Path(__file__).with_name("pcb_worker.py")
    try:
        record = _run([interpreter, str(worker), str(source)], timeout)
        result = json.loads(record["stdout"])
    finally:
        _source_unchanged(source, original)
    result.update({"source": str(source), "source_sha256": original, "source_unchanged": True,
                   "python_executable": interpreter, "rules": _project_rules(source)})
    if record["stderr"]:
        result["native_stderr"] = record["stderr"]
    return result


def _check(source: Path, output_dir: str | os.PathLike[str], executable: str | os.PathLike[str] | None,
           timeout: float, kind: str) -> dict[str, Any]:
    _validate_timeout(timeout)
    tool = resolve_kicad_cli(executable)
    output = _prepare_output(output_dir)
    original = _sha(source)
    report = output / f"{kind}.json"
    command = [tool, "pcb" if kind == "drc" else "sch", kind,
               "--format", "json", "--severity-all", "--output", str(report)]
    parity = kind == "drc" and source.with_suffix(".kicad_sch").exists()
    if kind == "drc":
        command.append("--all-track-errors")
        if parity:
            command.append("--schematic-parity")
    command.append(str(source))
    try:
        record = _run(command, timeout)
        native = json.loads(report.read_text(encoding="utf-8"))
    finally:
        _source_unchanged(source, original)
    groups = {"violations": native.get("violations", []),
              "unconnected_items": native.get("unconnected_items", []),
              "schematic_parity": native.get("schematic_parity", [])}
    if kind == "erc":
        groups = {"violations": [violation for sheet in native.get("sheets", [])
                                 for violation in sheet.get("violations", [])]}
    result = _base(source, original, tool, [record])
    result.update({"operation": kind, "report": str(report), "native_report": native,
                   "reported_counts": {k: len(v) for k, v in groups.items()},
                   "reported_severities": dict(Counter(str(v.get("severity", "unknown"))
                       for group in groups.values() for v in group)),
                   "schematic_parity_requested": parity if kind == "drc" else None,
                   "completed": True, "passed": all(not group for group in groups.values()),
                   "ignored_checks": native.get("ignored_checks", []),
                   "included_severities": native.get("included_severities", [])})
    result["limitations"].append("Ignored rules may not appear even with severity-all; review rules and exclusions.")
    return _finish(output, result)


def run_drc(board_path: str | os.PathLike[str], output_dir: str | os.PathLike[str],
            executable: str | os.PathLike[str] | None = None, timeout: float = 120) -> dict[str, Any]:
    _validate_timeout(timeout)
    return _check(_input(board_path, ".kicad_pcb"), output_dir, executable, timeout, "drc")


def run_erc(schematic_path: str | os.PathLike[str], output_dir: str | os.PathLike[str],
            executable: str | os.PathLike[str] | None = None, timeout: float = 120) -> dict[str, Any]:
    _validate_timeout(timeout)
    return _check(_input(schematic_path, ".kicad_sch"), output_dir, executable, timeout, "erc")


def export_netlist(schematic_path: str | os.PathLike[str], output_dir: str | os.PathLike[str],
                   executable: str | os.PathLike[str] | None = None, timeout: float = 120) -> dict[str, Any]:
    _validate_timeout(timeout)
    source = _input(schematic_path, ".kicad_sch")
    tool = resolve_kicad_cli(executable)
    output = _prepare_output(output_dir)
    original = _sha(source)
    target = output / "netlist.xml"
    try:
        record = _run([tool, "sch", "export", "netlist", "--format", "kicadxml",
                       "--output", str(target), str(source)], timeout)
        if not target.is_file() or not target.stat().st_size:
            raise RuntimeError("KiCad did not produce a nonempty netlist")
    finally:
        _source_unchanged(source, original)
    result = _base(source, original, tool, [record])
    result.update({"operation": "export_netlist", "netlist": str(target), "format": "kicadxml"})
    return _finish(output, result)


def export_gerbers(board_path: str | os.PathLike[str], output_dir: str | os.PathLike[str],
                   executable: str | os.PathLike[str] | None = None, timeout: float = 120) -> dict[str, Any]:
    """Export all copper plus mask, silk, paste, outline and separate PTH/NPTH drill.

    Does not infer fabrication readiness, refill zones, or run DRC implicitly.
    """
    _validate_timeout(timeout)
    source = _input(board_path, ".kicad_pcb")
    tool = resolve_kicad_cli(executable)
    output = _prepare_output(output_dir)
    original = _sha(source)
    # KiCad's wildcard includes all enabled internal copper layers; hard-coding
    # only F.Cu/B.Cu would silently omit inner layers on multilayer boards.
    layers = "*.Cu,F.Mask,B.Mask,F.Silkscreen,B.Silkscreen,F.Paste,B.Paste,Edge.Cuts"
    try:
        records = [_run([tool, "pcb", "export", "gerbers", "--layers", layers,
                         "--output", str(output) + os.sep, str(source)], timeout)]
        records.append(_run([tool, "pcb", "export", "drill", "--format", "excellon",
                             "--drill-origin", "absolute", "--excellon-units", "mm",
                             "--excellon-separate-th", "--output", str(output) + os.sep,
                             str(source)], timeout))
        if not any(output.glob("*.gbr")) and not any(output.glob("*.gtl")):
            raise RuntimeError("KiCad did not produce Gerber copper files")
        if not any(output.glob("*.drl")):
            raise RuntimeError("KiCad did not produce Excellon drill files")
    finally:
        _source_unchanged(source, original)
    result = _base(source, original, tool, records)
    result.update({"operation": "export_gerbers", "requested_layers": layers,
                   "gerber_x2": True, "net_attributes": True,
                   "drill_format": "excellon", "coordinate_origin": "absolute",
                   "pth_npth_separate": True})
    result["limitations"].append("No DRC or Gerber-to-copper comparison is implicit; inspect manufacturing output before fabrication.")
    return _finish(output, result)
