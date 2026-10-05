"""Unit tests plus opt-in smoke tests against installed native KiCad.

Select pytest -m integration to create synthetic CAD in a temporary directory
and exercise pcbnew, DRC, ERC, netlist and manufacturing exports. No user CAD is read.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch

import pytest

from kicad_mcp import kicad


class KiCadUnitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.board = self.root / "sample.kicad_pcb"
        self.board.write_text("synthetic input", encoding="utf-8")

    def test_output_refuses_existing_data(self):
        output = self.root / "out"
        output.mkdir()
        sentinel = output / "old.gbr"
        sentinel.write_text("never overwrite", encoding="utf-8")
        with patch.object(kicad, "resolve_kicad_cli", return_value="kicad-cli"):
            with self.assertRaises(ValueError):
                kicad.run_drc(self.board, output)
        self.assertEqual(sentinel.read_text(), "never overwrite")

    def test_empty_output_is_accepted_and_symlink_refused(self):
        output = self.root / "out"
        output.mkdir()
        self.assertEqual(kicad._prepare_output(output), output.resolve())
        alias = self.root / "alias"
        alias.symlink_to(output, target_is_directory=True)
        with self.assertRaises(ValueError):
            kicad._prepare_output(alias)

    def test_concurrent_empty_output_claim_allows_only_one_owner(self):
        output = self.root / "shared"
        output.mkdir()
        start = Barrier(2)

        def claim():
            start.wait(timeout=5)
            try:
                return kicad._prepare_output(output)
            except (ValueError, FileExistsError):
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: claim(), range(2)))
        self.assertEqual(sum(result is not None for result in results), 1)
        marker = output / ".kicad-mcp-output.lock"
        content = marker.read_text()
        self.assertIn("Reserved by KiCadMCP", content)
        with self.assertRaises(ValueError):
            kicad._prepare_output(output)
        self.assertEqual(marker.read_text(), content)

    def test_explicit_bad_executable_does_not_fall_back(self):
        with self.assertRaises(FileNotFoundError):
            kicad.resolve_kicad_cli(self.root / "missing")

    def test_invalid_input_type(self):
        with self.assertRaises(ValueError):
            kicad.run_erc(self.board, self.root / "out")

    def test_native_findings_and_ignored_checks_are_preserved(self):
        project = {"board": {"design_settings": {"rule_severities": {"clearance": "ignore"},
                       "drc_exclusions": ["old exclusion"]}}}
        self.board.with_suffix(".kicad_pro").write_text(json.dumps(project))
        self.board.with_suffix(".kicad_sch").write_text("synthetic schematic")

        def run(command, timeout):
            self.assertNotIn("--refill-zones", command)
            self.assertNotIn("--save-board", command)
            self.assertIn("--schematic-parity", command)
            self.assertIn("--severity-all", command)
            target = Path(command[command.index("--output") + 1])
            target.write_text(json.dumps({"violations": [], "unconnected_items": [
                {"severity": "error", "type": "unconnected_items"}], "schematic_parity": [],
                "ignored_checks": [{"key": "clearance"}]}))
            return {"command": command, "returncode": 0, "stdout": "", "stderr": ""}

        with patch.object(kicad, "resolve_kicad_cli", return_value="kicad-cli"), patch.object(kicad, "_run", side_effect=run):
            result = kicad.run_drc(self.board, self.root / "out")
        self.assertFalse(result["passed"])
        self.assertEqual(result["reported_counts"]["unconnected_items"], 1)
        self.assertEqual(result["rules"]["ignored_checks"]["drc"], ["clearance"])
        self.assertEqual(result["ignored_checks"], [{"key": "clearance"}])
        self.assertTrue(result["source_unchanged"])
        self.assertTrue(Path(result["manifest"]).exists())

    def test_source_change_fails_even_after_command_success(self):
        def run(command, timeout):
            self.board.write_text("changed")
            Path(command[command.index("--output") + 1]).write_text("{}")
            return {"command": command, "returncode": 0, "stdout": "", "stderr": ""}
        with patch.object(kicad, "resolve_kicad_cli", return_value="kicad-cli"), patch.object(kicad, "_run", side_effect=run):
            with self.assertRaisesRegex(RuntimeError, "Source file changed"):
                kicad.run_drc(self.board, self.root / "out")

    def test_invalid_timeouts_fail_before_subprocess_or_output_creation(self):
        operations = [
            lambda t: kicad.inspect_board(self.board, timeout=t),
            lambda t: kicad.run_drc(self.board, self.root / "drc", timeout=t),
            lambda t: kicad.run_erc(self.root / "missing.kicad_sch", self.root / "erc", timeout=t),
            lambda t: kicad.export_netlist(self.root / "missing.kicad_sch", self.root / "net", timeout=t),
            lambda t: kicad.export_gerbers(self.board, self.root / "gerber", timeout=t),
            lambda t: kicad.resolve_pcbnew_python(timeout=t),
            lambda t: kicad._run(["unused"], t),
        ]
        with patch.object(kicad.subprocess, "run") as subprocess_run:
            for value in (float("nan"), float("inf"), float("-inf"), 0, -1):
                for operation in operations:
                    with self.subTest(timeout=value, operation=operation):
                        with self.assertRaisesRegex(ValueError, "positive finite"):
                            operation(value)
            subprocess_run.assert_not_called()
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["sample.kicad_pcb"])

    def test_timeout_becomes_actionable_error(self):
        with patch.object(kicad.subprocess, "run", side_effect=subprocess.TimeoutExpired("test", 1)):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                kicad._run(["test"], 1)

    def test_erc_counts_all_sheets(self):
        schematic = self.board.with_suffix(".kicad_sch")
        schematic.write_text("test")
        def run(command, timeout):
            target = Path(command[command.index("--output") + 1])
            target.write_text(json.dumps({"sheets": [{"violations": []},
                {"violations": [{"severity": "warning", "type": "unconnected_pin"}]}]}))
            return {"command": command, "returncode": 0, "stdout": "", "stderr": ""}
        with patch.object(kicad, "resolve_kicad_cli", return_value="kicad-cli"), patch.object(kicad, "_run", side_effect=run):
            result = kicad.run_erc(schematic, self.root / "out")
        self.assertEqual(result["reported_counts"]["violations"], 1)
        self.assertFalse(result["passed"])


# The synthetic four-layer board intentionally contains an unconnected pad pair.
# Tests verify findings are returned rather than hardcoding a clean DRC result.
_CREATE_BOARD = r'''
import sys
if sys.platform == "darwin" or sys.platform == "win32":
    import wx
    app = wx.App(False)
import pcbnew as p
b = p.BOARD()
b.SetCopperLayerCount(4)
for start, end in [((0,0),(20,0)), ((20,0),(20,10)), ((20,10),(0,10)), ((0,10),(0,0))]:
    edge=p.PCB_SHAPE(b); edge.SetShape(p.SHAPE_T_SEGMENT); edge.SetLayer(p.Edge_Cuts)
    edge.SetStart(p.VECTOR2I(p.FromMM(start[0]),p.FromMM(start[1])))
    edge.SetEnd(p.VECTOR2I(p.FromMM(end[0]),p.FromMM(end[1])))
    edge.SetWidth(p.FromMM(.05)); b.Add(edge)
net=p.NETINFO_ITEM(b,"TEST_NET"); b.Add(net)
fp=p.FOOTPRINT(b); fp.SetReference("J1"); fp.SetValue("Synthetic test connector")
for number,x in [("1",5),("2",10)]:
    pad=p.PAD(fp); pad.SetNumber(number); pad.SetAttribute(p.PAD_ATTRIB_PTH)
    pad.SetShape(p.PAD_SHAPE_CIRCLE); pad.SetSize(p.VECTOR2I(p.FromMM(2),p.FromMM(2)))
    pad.SetDrillSize(p.VECTOR2I(p.FromMM(.8),p.FromMM(.8))); pad.SetLayerSet(p.LSET.AllCuMask())
    pad.SetPosition(p.VECTOR2I(p.FromMM(x),p.FromMM(5))); pad.SetNet(net); fp.Add(pad)
b.Add(fp); p.SaveBoard(sys.argv[1],b)
'''

_EMPTY_SCHEMATIC = '''(kicad_sch (version 20250114) (generator "eeschema")
(uuid "b45a48c8-c2b9-45bc-8eaf-721629bcf2ff") (paper "A4") (lib_symbols)
(sheet_instances (path "/" (page "1"))) (embedded_fonts no))'''


@pytest.mark.integration
class KiCadIntegrationTests(unittest.TestCase):
    def test_native_generated_board_and_schematic(self):
        try:
            interpreter = kicad.resolve_pcbnew_python()
            kicad.resolve_kicad_cli()
        except (FileNotFoundError, RuntimeError) as exc:
            self.skipTest(f"Native KiCad unavailable: {exc}")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            board = root / "synthetic.kicad_pcb"
            subprocess.run([interpreter, "-c", _CREATE_BOARD, str(board)], check=True,
                           capture_output=True, text=True, timeout=60)
            original = hashlib.sha256(board.read_bytes()).hexdigest()
            inspected = kicad.inspect_board(board, interpreter)
            self.assertEqual(inspected["counts"]["footprints"], 1)
            self.assertEqual(inspected["counts"]["pads"], 2)
            self.assertEqual(inspected["board_outline"]["edge_graphic_count"], 4)
            self.assertTrue(inspected["board_outline"]["valid_closed_outline"])
            self.assertAlmostEqual(inspected["board_outline"]["bounding_box_mm"]["width_mm"], 20, delta=.1)
            self.assertIn("In1.Cu", {layer["name"] for layer in inspected["enabled_layers"]})
            drc = kicad.run_drc(board, root / "drc")
            self.assertFalse(drc["passed"])
            self.assertGreater(drc["reported_counts"]["unconnected_items"], 0)
            self.assertFalse(drc["schematic_parity_requested"])
            exported = kicad.export_gerbers(board, root / "gerber")
            files = [Path(item["path"]).name for item in exported["files"]]
            # Protel extensions for internal layers vary between KiCad versions;
            # inspect the contents to prove four copper files were generated.
            copper = [Path(item["path"]).read_text(errors="replace") for item in exported["files"]
                      if Path(item["path"]).suffix.lower() not in {".gbrjob", ".drl"}]
            self.assertEqual(sum("FileFunction,Copper" in content for content in copper), 4, files)
            self.assertEqual(hashlib.sha256(board.read_bytes()).hexdigest(), original)
            schematic = root / "empty.kicad_sch"
            schematic.write_text(_EMPTY_SCHEMATIC, encoding="utf-8")
            erc = kicad.run_erc(schematic, root / "erc")
            self.assertTrue(erc["completed"])
            netlist = kicad.export_netlist(schematic, root / "netlist")
            self.assertIn("<export", Path(netlist["netlist"]).read_text())


if __name__ == "__main__":
    unittest.main()
