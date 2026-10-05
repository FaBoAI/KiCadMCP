"""Read-only KiCad geometry extraction, run by KiCad's own Python.

This module deliberately has no package/third-party imports except pcbnew/wx.
It never calls SaveBoard, ZONE_FILLER, or any editing operation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys


def extract(board_path: Path, net_name: str) -> dict:
    if sys.platform == "darwin":
        import wx
        app = wx.App(False)  # Keep alive until process exit for native geometry APIs.
    import pcbnew as pcb

    original = board_path.read_bytes()
    board = pcb.LoadBoard(str(board_path))
    report = {
        "source": str(board_path),
        "source_sha256": hashlib.sha256(original).hexdigest(),
        "net_name": net_name,
        "kicad_version": pcb.Version(),
        "coordinate_convention": "KiCad x,y in mm; y downward; no mirror or offset",
        "native_polygon_max_error_mm": 0.0005,
        "native_polygon_approximation": "inside; no tolerance used to bridge gaps",
        "saved_fills_only": True,
        "status": "extracted",
        "warnings": [],
        "rows": [],
        "holes": [],
        "zones": [],
    }
    if board.GetCopperLayerCount() != 2:
        report.update(status="unsupported", reason="Only two-layer boards are supported")
        return report
    layers = [pcb.F_Cu, pcb.B_Cu]
    net = board.FindNet(net_name)
    if net is None or net.GetNetCode() <= 0:
        report.update(status="limited", reason="Requested named net is absent")
        return report

    def xy(point):
        return [pcb.ToMM(point.x), pcb.ToMM(point.y)]

    def points(chain):
        if chain.ArcCount():
            raise ValueError("Unflattened native polygon arcs are unsupported")
        return [xy(chain.CPoint(i)) for i in range(chain.PointCount())]

    def polygons(polyset):
        return [
            {"outline": points(polyset.Outline(i)),
             "holes": [points(polyset.Hole(i, j)) for j in range(polyset.HoleCount(i))]}
            for i in range(polyset.OutlineCount())
        ]

    def copper(item, layer):
        polyset = pcb.SHAPE_POLY_SET()
        # Inscribed approximation is conservative for connectivity. In particular,
        # outward chord approximations must not turn a tangent into an overlap.
        item.TransformShapeToPolygon(
            polyset, layer, 0, pcb.FromMM(0.0005), pcb.ERROR_INSIDE
        )
        return polygons(polyset)

    def hole(uid, position, size, angle=0):
        row = {"id": uid, "xy_mm": xy(position), "drill_mm": xy(size),
               "angle_deg": angle}
        report["holes"].append(row)
        return row

    for zone in board.Zones():
        if zone.GetIsRuleArea() or zone.GetNetname() != net_name:
            continue
        for layer in layers:
            if not zone.IsOnLayer(layer):
                continue
            uid = "zone:" + zone.m_Uuid.AsString()
            poly = polygons(zone.GetFilledPolysList(layer))
            filled = bool(zone.IsFilled() and poly)
            report["zones"].append({"id": uid, "layer": pcb.LayerName(layer),
                                    "saved_fill_present": filled})
            if not filled:
                report["warnings"].append("Missing saved zone fill: " + uid)
            report["rows"].append({"id": uid, "kind": "zone",
                                    "layer": pcb.LayerName(layer), "polygons": poly})

    for footprint in board.GetFootprints():
        for pad in footprint.Pads():
            uid = "pad:" + pad.m_Uuid.AsString()
            drill = pad.GetDrillSize()
            drilled = bool(drill.x or drill.y)
            hole_data = None
            if drilled:
                if not drill.x or not drill.y:
                    report["warnings"].append("Invalid drill dimensions: " + uid)
                else:
                    hole_data = hole(uid, pad.GetPosition(), drill, pad.GetOrientationDegrees())
            if pad.GetNetname() != net_name:
                continue
            if pad.GetAttribute() == pcb.PAD_ATTRIB_NPTH:
                report["warnings"].append("Named net on non-plated hole: " + uid)
                continue
            for layer in layers:
                if not pad.IsOnLayer(layer):
                    continue
                if not pad.FlashLayer(layer):
                    report["warnings"].append("Unflashed pad/barrel requires separate modeling: " + uid)
                    continue
                row = {"id": uid, "kind": "pad", "reference": footprint.GetReference(),
                       "pin": pad.GetNumber(), "xy_mm": xy(pad.GetPosition()),
                       "layer": pcb.LayerName(layer), "polygons": copper(pad, layer),
                       "plated_through": bool(drilled and pad.GetAttribute() == pcb.PAD_ATTRIB_PTH)}
                if hole_data:
                    row["hole_id"] = uid
                report["rows"].append(row)

    for item in board.GetTracks():
        via = isinstance(item, pcb.PCB_VIA)
        uid = ("via:" if via else "track:") + item.m_Uuid.AsString()
        if via:
            d = item.GetDrillValue()
            hole(uid, item.GetPosition(), pcb.VECTOR2I(d, d))
        if item.GetNetname() != net_name:
            continue
        for layer in layers:
            if not item.IsOnLayer(layer):
                continue
            if via and not item.FlashLayer(layer):
                report["warnings"].append("Unflashed via/barrel requires separate modeling: " + uid)
                continue
            row = {"id": uid, "kind": "via" if via else "track",
                   "layer": pcb.LayerName(layer), "polygons": copper(item, layer),
                   "plated_through": via}
            if via:
                row["hole_id"] = uid
                row["xy_mm"] = xy(item.GetPosition())
            report["rows"].append(row)

    # Netless copper graphics may bridge otherwise disconnected net objects.
    # Do not assign them to this net merely because geometry happens to touch.
    for item in list(board.GetDrawings()) + [
        item for fp in board.GetFootprints() for item in fp.GraphicalItems()
    ]:
        if item.GetLayer() in layers:
            report["warnings"].append("Copper graphic/text not assigned to a net is outside audit scope")
            break
    if board_path.read_bytes() != original:
        raise RuntimeError("Source PCB changed during extraction")
    report["source_unchanged"] = True
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("board", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--net", default="GND")
    args = parser.parse_args()
    try:
        result = extract(args.board.resolve(), args.net)
        args.output.write_text(json.dumps(result, ensure_ascii=False) + "\n", encoding="utf-8")
    except Exception as exc:
        print(f"Geometry extraction failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 2
    print(json.dumps({"status": result["status"], "rows": len(result["rows"])}), flush=True)
    return 0


if __name__ == "__main__":
    code = main()
    # Some pcbnew/wx builds crash during interpreter teardown. All file writes
    # have already completed; no board mutation or pending background work exists.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
