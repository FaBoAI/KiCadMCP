"""Conservative, read-only topology audit of a saved two-layer PCB net."""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
import subprocess


def _geometry_imports():
    try:
        import shapely
        from shapely import make_valid
        from shapely.geometry import LineString, Point, Polygon
        from shapely import affinity
        from shapely.strtree import STRtree
    except ImportError as exc:
        raise RuntimeError("Ground auditing requires the optional 'geometry' dependency: pip install '.[geometry]'") from exc
    if int(shapely.__version__.split(".", 1)[0]) < 2:
        raise RuntimeError("Ground auditing requires Shapely >= 2")
    return shapely, make_valid, LineString, Point, Polygon, affinity, STRtree


def _polygon_parts(geometry):
    if geometry.is_empty:
        return []
    if geometry.geom_type == "Polygon":
        return [geometry]
    return [p for g in getattr(geometry, "geoms", []) for p in _polygon_parts(g)]


def _audit_geometry(raw: dict) -> dict:
    """Analyze native-worker JSON. Separate from extraction for deterministic tests."""
    shapely, make_valid, LineString, Point, Polygon, affinity, STRtree = _geometry_imports()
    report = {
        "status": raw["status"], "passed": False,
        "source": raw["source"], "source_sha256": raw["source_sha256"],
        "net_name": raw["net_name"], "kicad_version": raw.get("kicad_version"),
        "shapely_version": shapely.__version__, "saved_fills_only": True,
        "warnings": list(raw.get("warnings", [])),
        "method": "Individual copper polygons connect only through positive-area overlap. "
                  "All drill voids are removed. Plated annuli touching the same hole barrel bridge layers. "
                  "Point/line-only contacts are never used to connect components.",
        "scope": "DC topology of saved net-assigned copper on a two-layer PCB. "
                 "Not DRC, impedance, current capacity, thermal, EMI or manufacturing verification. "
                 "The tool cannot prove saved zone fills are up to date.",
        "minimum_overlap_area_mm2": 1e-9,
        "native_polygon_max_error_mm": raw.get("native_polygon_max_error_mm"),
        "coordinate_convention": raw.get("coordinate_convention"),
        "zone_count": len(raw.get("zones", [])),
    }
    if raw["status"] != "extracted":
        report["reason"] = raw.get("reason", "Native extraction incomplete")
        return report

    holes = {}
    for h in raw.get("holes", []):
        dx, dy = h["drill_mm"]
        if dx <= 0 or dy <= 0:
            report["warnings"].append("Invalid hole dimensions: " + h["id"])
            continue
        radius = min(dx, dy) / 2
        if dx > dy:
            center = LineString([(-(dx - dy) / 2, 0), ((dx - dy) / 2, 0)])
        elif dy > dx:
            center = LineString([(0, -(dy - dx) / 2), (0, (dy - dx) / 2)])
        else:
            center = Point(0, 0)
        # Circumscribed void: polygon chords must not leave false copper in a hole.
        shape = center.buffer(radius / math.cos(math.pi / 256), quad_segs=64)
        shape = affinity.rotate(shape, -h.get("angle_deg", 0), origin=(0, 0))
        holes[h["id"]] = affinity.translate(shape, *h["xy_mm"])
    hole_ids = list(holes)
    hole_tree = STRtree([holes[key] for key in hole_ids])
    nodes, row_nodes, repairs = [], [], []
    for ri, row in enumerate(raw["rows"]):
        associated = []
        for poly in row["polygons"]:
            shape = Polygon(poly["outline"], poly.get("holes", []))
            if not shape.is_valid:
                repairs.append({"id": row["id"], "layer": row["layer"]})
                shape = make_valid(shape)
            for hi in hole_tree.query(shape, predicate="intersects"):
                shape = shape.difference(holes[hole_ids[int(hi)]])
            for part in _polygon_parts(shape):
                if part.area <= 1e-9:
                    report["warnings"].append("Copper polygon below audit area resolution: " + row["id"])
                    continue
                associated.append(len(nodes))
                nodes.append({"geometry": part, "layer": row["layer"], "row": ri})
        row_nodes.append(associated)
        if row["kind"] == "pad" and not associated:
            report["warnings"].append("Pad has no modeled copper: " + row["id"])
    if repairs:
        report["warnings"].append("Invalid native polygons required repair; result is limited")
    report["polygon_repairs"] = repairs
    if not nodes:
        report.update(status="limited", reason="Named net has no modeled copper")
        return report

    parent = list(range(len(nodes)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def join(i, j):
        i, j = find(i), find(j)
        if i != j:
            parent[j] = i

    ignored = []
    for layer in ("F.Cu", "B.Cu"):
        indices = [i for i, node in enumerate(nodes) if node["layer"] == layer]
        tree = STRtree([nodes[i]["geometry"] for i in indices])
        for i in indices:
            a = nodes[i]["geometry"]
            for hit in tree.query(a, predicate="intersects"):
                j = indices[int(hit)]
                if j <= i:
                    continue
                overlap = a.intersection(nodes[j]["geometry"])
                if overlap.area > 1e-9:
                    join(i, j)
                else:
                    ignored.append({"layer": layer, "a": i, "b": j,
                                    "intersection_type": overlap.geom_type,
                                    "area_mm2": overlap.area})

    plated_nodes = defaultdict(list)
    for row, ids in zip(raw["rows"], row_nodes):
        if not row.get("plated_through"):
            continue
        hole = holes.get(row.get("hole_id"))
        if hole is None:
            report["warnings"].append("Plated item missing modeled drill: " + row["id"])
            continue
        for i in ids:
            # A remote island within a custom PTH pad must not connect through
            # its UUID alone. The copper must actually reach the hole barrel.
            if nodes[i]["geometry"].boundary.intersection(hole.boundary).length > 1e-7:
                plated_nodes[row["hole_id"]].append(i)
    for ids in plated_nodes.values():
        for i in ids[1:]:
            join(ids[0], i)

    groups = defaultdict(list)
    for i in range(len(nodes)):
        groups[find(i)].append(i)
    ordered = sorted(groups.values(), key=lambda group: -sum(nodes[i]["geometry"].area for i in group))
    component_of = {i: ci for ci, group in enumerate(ordered) for i in group}
    pads = {}
    for row, ids in zip(raw["rows"], row_nodes):
        if row["kind"] != "pad":
            continue
        entry = pads.setdefault(row["id"], {
            "reference": row.get("reference", ""), "pin": row.get("pin", ""),
            "xy_mm": row.get("xy_mm"), "components": set(), "layers": set(),
        })
        entry["components"].update(component_of[i] for i in ids)
        entry["layers"].add(row["layer"])
    pad_rows = [{**row, "components": sorted(row["components"]), "layers": sorted(row["layers"])}
                for row in pads.values()]
    disconnected = [row for row in pad_rows if row["components"] != [0]]
    for contact in ignored:
        contact["connected_by_other_positive_area_path"] = find(contact["a"]) == find(contact["b"])
    if not pad_rows:
        report["warnings"].append("No terminal pads on the requested net")
    connected = len(ordered) == 1 and not disconnected and bool(pad_rows)
    report.update(
        status="limited" if report["warnings"] else ("passed" if connected else "failed"),
        passed=connected and not report["warnings"],
        copper_component_count=len(ordered), polygon_node_count=len(nodes),
        pad_count=len(pad_rows), all_pads_on_main_component=not disconnected,
        disconnected_pads=disconnected, pads=pad_rows,
        ignored_point_or_line_contacts=ignored,
        plated_holes_connecting_copper=len(plated_nodes),
        source_object_counts=dict(Counter(row["kind"] for row in raw["rows"])),
        components=[{"component": ci, "polygon_nodes": group,
                     "layers": sorted({nodes[i]["layer"] for i in group})}
                    for ci, group in enumerate(ordered)],
    )
    return report


def audit_ground(board_path, output_dir, net_name="GND", python_executable=None, timeout=120) -> dict:
    """Audit saved copper without changing/refilling the board.

    ``output_dir`` must be empty. Return a JSON-compatible report with status
    ``passed``, ``failed``, ``limited`` or ``unsupported``. Only ``passed=True``
    is a positive topology result; all other states require review.
    """
    _geometry_imports()
    if not isinstance(net_name, str) or not net_name.strip():
        raise ValueError("net_name must be a nonempty string")
    timeout = float(timeout)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be positive and finite")
    board = Path(board_path).expanduser().resolve()
    if not board.is_file() or board.suffix.lower() != ".kicad_pcb":
        raise ValueError("board_path must name an existing .kicad_pcb file")
    from .kicad import _prepare_output, resolve_pcbnew_python
    out = _prepare_output(output_dir)
    python = resolve_pcbnew_python(python_executable, timeout=min(float(timeout), 10))
    before = hashlib.sha256(board.read_bytes()).hexdigest()
    raw_path = out / "ground_geometry.json"
    command = [python, str(Path(__file__).with_name("ground_worker.py")),
               str(board), str(raw_path), "--net=" + net_name]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        (out / "ground_worker.log").write_text("Native extraction timed out\n", encoding="utf-8")
        raise RuntimeError("Native geometry extraction timed out; no audit passed") from exc
    (out / "ground_worker.log").write_text(result.stdout + result.stderr, encoding="utf-8")
    if result.returncode or not raw_path.is_file():
        raise RuntimeError(f"Native geometry extraction failed (exit {result.returncode}); see {out / 'ground_worker.log'}")
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    if raw["source_sha256"] != before:
        raise RuntimeError("Source PCB changed before native extraction")
    report = _audit_geometry(raw)
    if hashlib.sha256(board.read_bytes()).hexdigest() != before:
        raise RuntimeError("Source PCB changed during audit")
    report["source_unchanged"] = True
    report["geometry_file"] = str(raw_path)
    report["report_file"] = str(out / "ground_audit.json")
    (out / "ground_audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report
