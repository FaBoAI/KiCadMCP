"""Native solver smoke checks use only a generated passive RC test circuit."""
import math
from pathlib import Path

import pytest

from kicad_mcp.raw import read_raw
from kicad_mcp.spice import _executable, run_spice


@pytest.mark.integration
@pytest.mark.parametrize("engine", ["ltspice", "ngspice"])
def test_native_rc_solver(engine, tmp_path):
    try:
        executable = _executable(engine, None)
    except FileNotFoundError as exc:
        pytest.skip(str(exc))
    deck = tmp_path / "rc.cir"
    deck.write_text("RC smoke test\nV1 in 0 DC 1\nR1 in out 1k\nC1 out 0 1u IC=0\n"
                    ".tran 10u 5m 0 10u uic\n.end\n")
    original = deck.read_bytes()
    result = run_spice(str(deck), engine=engine, executable=str(executable),
                       timeout=30, output_dir=str(tmp_path / "runs"))
    assert result["completed"], result
    raw = read_raw(result["raw_path"], binary_format=engine)
    names = {name.casefold(): name for name in raw.variables}
    assert raw.vectors[names["v(out)"]][-1] == pytest.approx(1 - math.exp(-5), abs=2e-4)
    assert deck.read_bytes() == original
    assert Path(result["raw_path"]).is_relative_to(tmp_path / "runs")
