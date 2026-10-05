"""Local stdio MCP interface. Engines remain separately installed programs."""
from __future__ import annotations

import asyncio
from functools import wraps
from pathlib import Path

from . import __version__
from .paths import Workspace


def create_server(workspace: str | Path):
    try:
        from mcp.server import MCPServer
        from mcp.server.mcpserver.exceptions import ToolError
    except ImportError as exc:
        raise RuntimeError('Install the MCP extra: pip install ".[mcp]"') from exc
    from . import kicad, raw, spice

    def tool_errors(function):
        @wraps(function)
        async def wrapped(*args, **kwargs):
            try:
                return await function(*args, **kwargs)
            except (OSError, ValueError, RuntimeError, ImportError) as exc:
                raise ToolError(str(exc)) from exc
        return wrapped

    paths = Workspace(workspace)
    server = MCPServer(
        "KiCadMCP", version=__version__,
        instructions=("Inspect local KiCad designs and run trusted SPICE netlists. "
                      "All path arguments must stay inside the configured workspace. "
                      "No automatic PCB saving or zone refill. Numerical completion, "
                      "DRC results and physical circuit validation are different checks."),
    )

    @server.tool()
    @tool_errors
    async def inspect_board(board_path: str, timeout: float = 60) -> dict[str, object]:
        """Read PCB geometry, layers, references and nets without changing the board."""
        return await asyncio.to_thread(kicad.inspect_board, paths.path(board_path), timeout=timeout)

    @server.tool()
    @tool_errors
    async def run_drc(board_path: str, output_dir: str, timeout: float = 120) -> dict[str, object]:
        """Run native KiCad DRC; save JSON in a new/empty output directory."""
        return await asyncio.to_thread(kicad.run_drc, paths.path(board_path), paths.path(output_dir), timeout=timeout)

    @server.tool()
    @tool_errors
    async def run_erc(schematic_path: str, output_dir: str, timeout: float = 120) -> dict[str, object]:
        """Run native schematic ERC, keeping the original design unchanged."""
        return await asyncio.to_thread(kicad.run_erc, paths.path(schematic_path), paths.path(output_dir), timeout=timeout)

    @server.tool()
    @tool_errors
    async def export_netlist(schematic_path: str, output_dir: str, timeout: float = 120) -> dict[str, object]:
        """Export schematic connectivity as KiCad XML into a new/empty directory."""
        return await asyncio.to_thread(kicad.export_netlist, paths.path(schematic_path), paths.path(output_dir), timeout=timeout)

    @server.tool()
    @tool_errors
    async def export_gerbers(board_path: str, output_dir: str, timeout: float = 120) -> dict[str, object]:
        """Export Gerber/Excellon from saved copper; does not refill zones or approve fabrication."""
        return await asyncio.to_thread(kicad.export_gerbers, paths.path(board_path), paths.path(output_dir), timeout=timeout)

    @server.tool()
    @tool_errors
    async def audit_ground(board_path: str, output_dir: str, net_name: str = "GND", timeout: float = 120) -> dict[str, object]:
        """Audit saved two-layer ground copper, including drill holes and plated layer links."""
        from .ground import audit_ground as run
        return await asyncio.to_thread(run, paths.path(board_path), paths.path(output_dir), net_name=net_name, timeout=timeout)

    @server.tool()
    @tool_errors
    async def run_spice(netlist: str, output_dir: str, engine: str = "ltspice", timeout: float = 60) -> dict[str, object]:
        """Run a trusted netlist in an isolated output folder; report timeout and numerical completion separately."""
        return await asyncio.to_thread(spice.run_spice, paths.path(netlist), output_dir=paths.path(output_dir), engine=engine, timeout=timeout)

    @server.tool()
    @tool_errors
    async def raw_summary(raw_path: str, signals: list[str] | None = None, start: float | None = None,
                          end: float | None = None, binary_format: str = "auto") -> dict[str, object]:
        """Summarize selected real RAW signals in an optional time window; no design pass/fail inference."""
        return await asyncio.to_thread(raw.summarize_raw, paths.path(raw_path), signals=signals, start=start, end=end, binary_format=binary_format)

    @server.tool()
    @tool_errors
    async def raw_csv(raw_path: str, output_file: str, signals: list[str] | None = None,
                      binary_format: str = "auto") -> dict[str, object]:
        """Write selected saved RAW points as a new CSV file; parent directory must exist."""
        output = await asyncio.to_thread(raw.write_csv, paths.path(raw_path), paths.path(output_file),
                                         signals=signals, binary_format=binary_format)
        return {"csv_path": output}

    return server


def serve(workspace: str | Path) -> None:
    create_server(workspace).run(transport="stdio")
