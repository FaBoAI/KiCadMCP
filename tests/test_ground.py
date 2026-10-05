"""Synthetic geometry and native PCB fixtures; no production board data."""
import hashlib
import json
from pathlib import Path
import subprocess

import pytest

pytest.importorskip("shapely", minversion="2")
from kicad_mcp.ground import _audit_geometry, audit_ground


def polygon(x0, y0, x1, y1):
    return {"outline": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]], "holes": []}


def row(uid, shape, layer="F.Cu", kind="pad", **extra):
    return {"id": uid, "kind": kind, "reference": uid, "pin": "1", "layer": layer,
            "xy_mm": [0, 0], "polygons": [shape], **extra}


def raw(rows, holes=(), **overrides):
    return {"source": "synthetic.kicad_pcb", "source_sha256": "0" * 64,
            "net_name": "GND", "status": "extracted", "warnings": [],
            "rows": rows, "holes": list(holes), "zones": [], **overrides}


def test_positive_overlap_passes():
    result = _audit_geometry(raw([row("a", polygon(0, 0, 2, 2)), row("b", polygon(1, 0, 3, 2))]))
    assert result["passed"]
    assert result["copper_component_count"] == 1


@pytest.mark.parametrize("shape,kind", [(polygon(2, 2, 3, 3), "Point"), (polygon(2, 0, 3, 2), "LineString")])
def test_point_and_line_contacts_never_connect(shape, kind):
    result = _audit_geometry(raw([row("a", polygon(0, 0, 2, 2)), row("b", shape)]))
    assert not result["passed"]
    assert result["status"] == "failed"
    assert result["copper_component_count"] == 2
    assert result["ignored_point_or_line_contacts"][0]["intersection_type"] == kind


def test_positive_alternate_path_can_connect_point_contact():
    result = _audit_geometry(raw([row("a", polygon(0, 0, 2, 2)), row("b", polygon(2, 2, 4, 4)),
                                  row("bridge", polygon(1, 1, 3, 3), kind="track")]))
    assert result["passed"]
    assert result["ignored_point_or_line_contacts"][0]["connected_by_other_positive_area_path"]


def test_isolated_copper_without_pad_fails():
    result = _audit_geometry(raw([row("a", polygon(0, 0, 2, 2)),
                                  row("island", polygon(5, 5, 6, 6), kind="zone")]))
    assert not result["passed"]
    assert result["all_pads_on_main_component"]
    assert result["copper_component_count"] == 2


def test_drill_void_removes_false_copper_connection():
    # A large non-plated hole cuts completely across a narrow copper strip.
    hole = {"id": "hole", "xy_mm": [2, 0], "drill_mm": [1, 1]}
    result = _audit_geometry(raw([row("strip", polygon(0, -0.2, 4, 0.2))], [hole]))
    assert not result["passed"]
    assert result["copper_component_count"] == 2


def test_plated_annulus_connects_front_and_back():
    hole = {"id": "via", "xy_mm": [0, 0], "drill_mm": [0.4, 0.4]}
    rows = [row("via", polygon(-1, -1, 1, 1), layer=side, plated_through=True, hole_id="via")
            for side in ("F.Cu", "B.Cu")]
    result = _audit_geometry(raw(rows, [hole]))
    assert result["passed"]
    assert result["plated_holes_connecting_copper"] == 1


def test_same_xy_without_plating_does_not_connect_layers():
    rows = [row("a", polygon(-1, -1, 1, 1), layer=side) for side in ("F.Cu", "B.Cu")]
    assert _audit_geometry(raw(rows))["copper_component_count"] == 2


def test_custom_plated_pad_remote_island_not_joined_by_uuid():
    hole = {"id": "via", "xy_mm": [0, 0], "drill_mm": [0.4, 0.4]}
    rows = [row("via", polygon(-1, -1, 1, 1), layer=side, plated_through=True, hole_id="via")
            for side in ("F.Cu", "B.Cu")]
    rows[0]["polygons"].append(polygon(4, 4, 5, 5))
    result = _audit_geometry(raw(rows, [hole]))
    assert not result["passed"]
    assert result["copper_component_count"] == 2


@pytest.mark.parametrize("status", ["limited", "unsupported"])
def test_incomplete_extraction_never_passes(status):
    result = _audit_geometry(raw([], status=status, reason="synthetic limitation"))
    assert result["status"] == status
    assert not result["passed"]


def test_missing_zone_fill_makes_connected_result_limited():
    result = _audit_geometry(raw([row("a", polygon(0, 0, 1, 1))], warnings=["Missing saved zone fill"]))
    assert result["status"] == "limited"
    assert not result["passed"]


def test_no_terminal_pads_does_not_pass():
    result = _audit_geometry(raw([row("track", polygon(0, 0, 1, 1), kind="track")]))
    assert result["status"] == "limited"
    assert not result["passed"]


def test_empty_net_geometry_does_not_pass():
    result = _audit_geometry(raw([]))
    assert result["status"] == "limited"
    assert not result["passed"]


NATIVE_FIXTURE = r'''
import os, sys
from pathlib import Path
if sys.platform == 'darwin':
    import wx
    app = wx.App(False)
import pcbnew as p
out = Path(sys.argv[1])
def pt(x, y): return p.VECTOR2I(p.FromMM(x), p.FromMM(y))
def create(name, via=True, unfilled=False, multilayer=False, filled=False):
    b = p.BOARD()
    if multilayer: b.SetCopperLayerCount(4)
    net = p.NETINFO_ITEM(b, 'GND')
    b.Add(net)
    for ref, x, side in [('J1', 5, p.F_Cu), ('J2', 7, p.B_Cu)]:
        fp = p.FOOTPRINT(b); fp.SetReference(ref); b.Add(fp)
        pad = p.PAD(fp); pad.SetNumber('1'); pad.SetAttribute(p.PAD_ATTRIB_SMD)
        pad.SetShape(p.PAD_SHAPE_RECT); pad.SetSize(pt(1,1)); pad.SetPosition(pt(x,5))
        layers=p.LSET(); layers.AddLayer(side)
        pad.SetLayerSet(layers); pad.SetNet(net); fp.Add(pad)
    for x0,x1,layer in [(5,6,p.F_Cu),(6,7,p.B_Cu)]:
        t=p.PCB_TRACK(b); t.SetStart(pt(x0,5)); t.SetEnd(pt(x1,5)); t.SetWidth(p.FromMM(.3))
        t.SetLayer(layer); t.SetNet(net); b.Add(t)
    if via:
        v=p.PCB_VIA(b); v.SetPosition(pt(6,5)); v.SetWidth(p.FromMM(.7)); v.SetDrill(p.FromMM(.3))
        v.SetLayerPair(p.F_Cu,p.B_Cu); v.SetNet(net); b.Add(v)
    if unfilled or filled:
        z=p.ZONE(b); z.SetLayer(p.F_Cu); z.SetNet(net)
        z.Outline().NewOutline()
        for x,y in [(4,4),(8,4),(8,6),(4,6)]: z.Outline().Append(int(p.FromMM(x)),int(p.FromMM(y)))
        if filled:
            poly=p.SHAPE_POLY_SET(); poly.NewOutline()
            for x,y in [(4,4),(8,4),(8,6),(4,6)]: poly.Append(int(p.FromMM(x)),int(p.FromMM(y)))
            z.SetFilledPolysList(p.F_Cu,poly); z.SetIsFilled(True)
        b.Add(z)
    p.SaveBoard(str(out/(name+'.kicad_pcb')),b)
create('connected')
create('isolated',via=False)
create('unfilled',unfilled=True)
create('filled',filled=True)
create('fourlayer',multilayer=True)
def touch(name, point=False):
    b=p.BOARD(); net=p.NETINFO_ITEM(b,'GND'); b.Add(net)
    for ref,x,y in [('J1',5,5),('J2',6,6 if point else 5)]:
        fp=p.FOOTPRINT(b); fp.SetReference(ref); b.Add(fp)
        pad=p.PAD(fp); pad.SetNumber('1'); pad.SetAttribute(p.PAD_ATTRIB_SMD)
        pad.SetShape(p.PAD_SHAPE_RECT); pad.SetSize(pt(1,1)); pad.SetPosition(pt(x,y))
        ls=p.LSET(); ls.AddLayer(p.F_Cu); pad.SetLayerSet(ls); pad.SetNet(net); fp.Add(pad)
    p.SaveBoard(str(out/(name+'.kicad_pcb')),b)
touch('line_contact')
touch('point_contact',point=True)
def plated(name, slot=False):
    b=p.BOARD(); net=p.NETINFO_ITEM(b,'GND'); b.Add(net)
    fp=p.FOOTPRINT(b); fp.SetReference('J1'); b.Add(fp)
    pad=p.PAD(fp); pad.SetNumber('1'); pad.SetAttribute(p.PAD_ATTRIB_PTH)
    pad.SetShape(p.PAD_SHAPE_RECT); pad.SetSize(pt(2,2)); pad.SetPosition(pt(5,5))
    pad.SetDrillSize(pt(1.2,.4) if slot else pt(.4,.4))
    pad.SetDrillShape(p.PAD_DRILL_SHAPE_OBLONG if slot else p.PAD_DRILL_SHAPE_CIRCLE)
    pad.SetOrientationDegrees(45)
    ls=p.LSET(); ls.AddLayer(p.F_Cu); ls.AddLayer(p.B_Cu)
    pad.SetLayerSet(ls); pad.SetNet(net); fp.Add(pad)
    p.SaveBoard(str(out/(name+'.kicad_pcb')),b)
plated('pth_round')
plated('pth_slot',slot=True)
sys.stdout.flush()
os._exit(0)
'''


@pytest.fixture(scope="module")
def native_fixtures(tmp_path_factory):
    from kicad_mcp.kicad import resolve_pcbnew_python
    try:
        python = resolve_pcbnew_python(timeout=10)
    except (RuntimeError, FileNotFoundError) as exc:
        pytest.skip(f"KiCad native Python not installed: {exc}")
    folder = tmp_path_factory.mktemp("ground_native")
    script = folder / "create_fixture.py"
    script.write_text(NATIVE_FIXTURE, encoding="utf-8")
    result = subprocess.run([python, str(script), str(folder)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    return python, folder


@pytest.mark.integration
@pytest.mark.parametrize("name,status,passed", [
    ("connected", "passed", True), ("isolated", "failed", False),
    ("unfilled", "limited", False), ("fourlayer", "unsupported", False),
    ("filled", "passed", True),
    ("point_contact", "failed", False), ("line_contact", "failed", False),
    ("pth_round", "passed", True), ("pth_slot", "passed", True),
])
def test_native_saved_copper_audit(native_fixtures, tmp_path, name, status, passed):
    python, folder = native_fixtures
    board = folder / f"{name}.kicad_pcb"
    before = hashlib.sha256(board.read_bytes()).hexdigest()
    result = audit_ground(board, tmp_path / "audit", python_executable=python)
    assert result["status"] == status
    assert result["passed"] is passed
    assert result["source_unchanged"]
    assert hashlib.sha256(board.read_bytes()).hexdigest() == before
    assert json.loads(Path(result["report_file"]).read_text())["status"] == status


@pytest.mark.integration
def test_native_absent_net_is_limited(native_fixtures, tmp_path):
    python, folder = native_fixtures
    result = audit_ground(folder / "connected.kicad_pcb", tmp_path / "audit", net_name="NO_SUCH_NET", python_executable=python)
    assert result["status"] == "limited"
    assert not result["passed"]


@pytest.mark.integration
def test_reject_nonempty_output(native_fixtures, tmp_path):
    python, folder = native_fixtures
    (tmp_path / "old.txt").write_text("keep")
    with pytest.raises(ValueError, match="empty"):
        audit_ground(folder / "connected.kicad_pcb", tmp_path, python_executable=python)
    assert (tmp_path / "old.txt").read_text() == "keep"


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_invalid_timeout_rejected_before_native_operation(tmp_path, timeout):
    with pytest.raises(ValueError, match="positive and finite"):
        audit_ground(tmp_path / "unused.kicad_pcb", tmp_path / "output", timeout=timeout)
    assert not (tmp_path / "output").exists()
