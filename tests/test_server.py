import os
from pathlib import Path
import sys

import pytest

pytest.importorskip("mcp")
from mcp import Client, StdioServerParameters
from kicad_mcp.server import create_server


def waveform(path):
    path.write_text("Title: synthetic test\nPlotname: Transient Analysis\nFlags: real\n"
                    "No. Variables: 2\nNo. Points: 3\nVariables:\n"
                    "0 time time\n1 V(out) voltage\nValues:\n0 0 0\n1 1 2\n2 2 4\n")


@pytest.mark.asyncio
async def test_inprocess_protocol_and_path_guard(tmp_path):
    waveform(tmp_path / "wave.raw")
    async with Client(create_server(tmp_path)) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert {"run_drc", "run_erc", "audit_ground", "run_spice", "raw_summary", "export_gerbers", "raw_csv"} <= names
        result = await client.call_tool("raw_summary", {"raw_path": "wave.raw", "signals": ["V(out)"]})
        assert not result.is_error
        assert result.structured_content["signals"]["V(out)"]["axis_weighted_mean"] == 2.0
        escaped = await client.call_tool("raw_summary", {"raw_path": "../outside.raw"})
        assert escaped.is_error
        assert "inside" in escaped.content[0].text
        output = await client.call_tool("raw_csv", {"raw_path": "wave.raw", "output_file": "wave.csv"})
        assert not output.is_error
        assert (tmp_path / "wave.csv").read_text().splitlines()[0] == "time,V(out)"
        duplicate = await client.call_tool("raw_csv", {"raw_path": "wave.raw", "output_file": "wave.csv"})
        assert duplicate.is_error


@pytest.mark.asyncio
async def test_real_stdio_transport(tmp_path):
    waveform(tmp_path / "wave.raw")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    params = StdioServerParameters(command=sys.executable,
                                   args=["-m", "kicad_mcp", "serve", "--workspace", str(tmp_path)], env=env)
    async with Client(params, read_timeout_seconds=15) as client:
        assert len((await client.list_tools()).tools) == 9
        result = await client.call_tool("raw_summary", {"raw_path": "wave.raw", "start": 0.5, "end": 1.5})
        assert not result.is_error
        assert result.structured_content["signals"]["V(out)"]["min"] == 1.0
