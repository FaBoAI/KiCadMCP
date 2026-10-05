"""Command-line entry point; machine-readable JSON on stdout."""
from __future__ import annotations

import argparse
import json
import math
import sys

from . import __version__


def positive_timeout(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("timeout must be positive and finite")
    return number


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kicad-mcp", description=__doc__)
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("inspect", "drc", "erc", "netlist", "gerber", "ground"):
        q = sub.add_parser(name)
        q.add_argument("input", help="KiCad PCB or root schematic path")
        if name != "inspect":
            q.add_argument("--output", required=True, help="New/empty output directory")
        q.add_argument("--timeout", type=positive_timeout, default=120)
        if name in ("inspect", "ground"):
            q.add_argument("--python", dest="python_executable", help="Python executable that can import pcbnew")
        else:
            q.add_argument("--executable", help="kicad-cli executable")
        if name == "ground":
            q.add_argument("--net", default="GND")
    q = sub.add_parser("spice-run")
    q.add_argument("netlist")
    q.add_argument("--engine", choices=("ltspice", "ngspice"), default="ltspice")
    q.add_argument("--executable")
    q.add_argument("--timeout", type=positive_timeout, default=60)
    q.add_argument("--output", required=True)
    q = sub.add_parser("raw-summary")
    q.add_argument("raw_path")
    q.add_argument("--signals", nargs="+")
    q.add_argument("--start", type=float)
    q.add_argument("--end", type=float)
    q.add_argument("--binary-format", default="auto", choices=("auto", "ltspice", "ngspice"))
    q = sub.add_parser("raw-csv")
    q.add_argument("raw_path")
    q.add_argument("--output", required=True, help="New CSV file; parent directory must exist")
    q.add_argument("--signals", nargs="+")
    q.add_argument("--binary-format", default="auto", choices=("auto", "ltspice", "ngspice"))
    q = sub.add_parser("serve")
    q.add_argument("--workspace", required=True, help="Root directory for all MCP path arguments")
    return p


def dispatch(a: argparse.Namespace) -> dict | None:
    if a.command == "serve":
        from .server import serve
        serve(a.workspace)
        return None
    if a.command == "spice-run":
        from .spice import run_spice
        return run_spice(a.netlist, engine=a.engine, executable=a.executable, timeout=a.timeout, output_dir=a.output)
    if a.command == "raw-csv":
        from .raw import write_csv
        return {"csv_path": write_csv(a.raw_path, a.output, signals=a.signals, binary_format=a.binary_format)}
    if a.command == "raw-summary":
        from .raw import summarize_raw
        return summarize_raw(a.raw_path, signals=a.signals, start=a.start, end=a.end, binary_format=a.binary_format)
    if a.command == "ground":
        from .ground import audit_ground
        return audit_ground(a.input, a.output, net_name=a.net, python_executable=a.python_executable, timeout=a.timeout)
    from . import kicad
    if a.command == "inspect":
        return kicad.inspect_board(a.input, python_executable=a.python_executable, timeout=a.timeout)
    name = {"drc": "run_drc", "erc": "run_erc", "netlist": "export_netlist", "gerber": "export_gerbers"}[a.command]
    return getattr(kicad, name)(a.input, a.output, executable=a.executable, timeout=a.timeout)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        result = dispatch(args)
        if result is None:
            return 0
        print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
        if args.command == "spice-run" and not result.get("completed", False):
            return 1
        if result.get("passed") is False or result.get("numerical_complete") is False:
            return 1
        return 0
    except (OSError, RuntimeError, ValueError, ImportError) as exc:
        print(json.dumps({"error": str(exc), "type": type(exc).__name__}, ensure_ascii=False), file=sys.stderr)
        return 2
