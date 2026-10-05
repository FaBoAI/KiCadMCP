"""Native pcbnew worker. Run with KiCad's Python, not the MCP server Python.

stdout is one JSON document. All CAD access is read-only; never call SaveBoard.
"""
from __future__ import annotations

from collections import Counter
import json
import os
from pathlib import Path
import sys


def inspect(path: str) -> dict:
    # macOS pcbnew accesses wx standard paths even for read-only board loads.
    # Avoid requiring a display server for headless Linux deployments.
    app = None
    if sys.platform == "darwin" or os.name == "nt":
        import wx
        app = wx.App(False)
    import pcbnew as p

    board = p.LoadBoard(path)
    if board is None:
        raise RuntimeError("pcbnew could not load the board")

    def xy(point):
        return [round(p.ToMM(point.x), 6), round(p.ToMM(point.y), 6)]

    def bbox(box):
        return {"x_mm": round(p.ToMM(box.GetX()), 6), "y_mm": round(p.ToMM(box.GetY()), 6),
                "width_mm": round(p.ToMM(box.GetWidth()), 6), "height_mm": round(p.ToMM(box.GetHeight()), 6)}

    def layer_name(layer):
        return board.GetLayerName(int(layer))

    pads = []
    footprints = []
    for footprint in board.GetFootprints():
        reference = footprint.GetReference()
        component_pads = []
        for pad in footprint.Pads():
            item = {"reference": reference, "number": pad.GetNumber(),
                    "net_code": pad.GetNetCode(), "net_name": pad.GetNetname(),
                    "position_mm": xy(pad.GetPosition()), "size_mm": xy(pad.GetSize()),
                    "drill_mm": xy(pad.GetDrillSize()), "attribute": int(pad.GetAttribute()),
                    "layers": [layer_name(layer) for layer in pad.GetLayerSet().Seq()]}
            component_pads.append(item)
            pads.append(item)
        footprints.append({"reference": reference, "value": footprint.GetValue(),
                           "library_id": ":".join(filter(None, [str(footprint.GetFPID().GetLibNickname()), str(footprint.GetFPID().GetLibItemName())])), "position_mm": xy(footprint.GetPosition()),
                           "rotation_degrees": footprint.GetOrientationDegrees(), "layer": footprint.GetLayerName(),
                           "dnp": bool(footprint.IsDNP()), "pad_count": len(component_pads), "pads": component_pads})
    drawings = list(board.GetDrawings())
    edge_count = sum(d.GetLayer() == p.Edge_Cuts for d in drawings)
    # Do not infer a rectangular outline when the board has no valid Edge.Cuts.
    polygon = p.SHAPE_POLY_SET()
    try:
        valid_outline = bool(board.GetBoardPolygonOutlines(polygon, False))
    except TypeError:  # KiCad 8/9 signature predates explicit inference flag.
        valid_outline = bool(board.GetBoardPolygonOutlines(polygon))
    outline_count = polygon.OutlineCount()
    tracks = list(board.GetTracks())
    nets = [{"code": int(code), "name": net.GetNetname()}
            for code, net in board.GetNetsByNetcode().items()]
    refs = Counter(footprint["reference"] for footprint in footprints)
    duplicate_refs = {ref: count for ref, count in refs.items() if count > 1}
    enabled_layers = [{"id": int(layer), "name": layer_name(layer)} for layer in board.GetEnabledLayers().Seq()]
    zones = list(board.Zones())
    return {
        "schema_version": 1, "kicad_version": p.Version(), "read_only": True,
        "board_outline": {"valid_closed_outline": valid_outline, "edge_graphic_count": edge_count,
                          "polygon_count": outline_count,
                          "hole_count": sum(polygon.HoleCount(i) for i in range(outline_count)),
                          "bounding_box_mm": bbox(board.GetBoardEdgesBoundingBox()) if edge_count else None},
        "enabled_layers": enabled_layers,
        "counts": {"footprints": len(footprints), "pads": len(pads),
                   "numbered_physical_pads": sum(bool(pad["number"]) for pad in pads),
                   "unique_reference_pad_pairs": len({(pad["reference"], pad["number"]) for pad in pads if pad["number"]}),
                   "nets_including_unassigned": len(nets), "nonempty_nets": sum(bool(net["name"]) for net in nets),
                   "tracks_and_vias": len(tracks), "vias": sum(isinstance(track, p.PCB_VIA) for track in tracks),
                   "zones": len(zones), "drawings": len(drawings)},
        "duplicate_references": duplicate_refs,
        "nets": sorted(nets, key=lambda n: n["code"]),
        "footprints": sorted(footprints, key=lambda item: item["reference"]),
        "limitations": ["Net assignments do not prove physical copper continuity.",
                        "The saved board is inspected without zone refill or modification.",
                        "Outline bounding box does not verify mechanical fit or connector placement."],
    }


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: pcb_worker.py BOARD.kicad_pcb", file=sys.stderr)
        return 2
    try:
        result = inspect(str(Path(sys.argv[1]).resolve(strict=True)))
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
