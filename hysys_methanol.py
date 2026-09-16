"""
Methanol-case specifics: which flowsheet objects to cache and how one operating point is run.
Generic HYSYS helpers (connection, units, inspection) live in ``hysys.py``.

Typical session::

    from hysys import open_case, dump
    from hysys_methanol import cache_objects, run_point

    case    = open_case(r"C:\\...\\methanol.hsc")
    objects = cache_objects(case)
    row     = run_point(objects, pressure=(90, "bar"), temperature=(250, "C"),
                        ratio=3.0, volume=(25, "m3"), purge_rate=0.05)
    dump(objects["reactor"])

Values ending in ``Value`` on HYSYS objects are plain numbers in HYSYS
internal units (kPa, C, m3, kg/s, kgmole/s, kJ/s).  Inputs to ``run_point``
may be given as bare numbers in those units or as ``(number, "unit")`` pairs,
which are converted through HYSYS's own unit tables.
"""
from __future__ import annotations

import time
from datetime import datetime

from hysys import to_internal, solve

__all__ = ["cache_objects", "run_point"]

# ---------------------------------------------------------------------------
# Flowsheet objects
# ---------------------------------------------------------------------------
def cache_objects(case) -> dict:
    """
    Look up the streams, unit operations and cells used by `run_point` once.

    Each attribute access on a COM proxy is a round trip into HYSYS, and
    lookups by name are among the slowest, so callers keep this dict for the
    life of the session and pass it to every run.
    """
    fs = case.Flowsheet
    streams, ops = fs.MaterialStreams, fs.Operations
    purge = streams("Purge")
    components = list(purge.FluidPackage.Components.Names)
    return {
        "solver": case.Solver,
        "units": case.Application.UnitConversionSetManager,
        # streams
        "co2in": streams("co2_1atm"),
        "h2in": streams("h2"),
        "rin": streams("RinV"),          # reactor inlet
        "rout": streams("RoutV"),        # reactor outlet
        "methanol": streams("Methanol"),
        "purge": purge,
        "recycle_gas": streams("VAPToMixer"),   # recycle after compression, into MIX-101
        # unit operations
        "reactor": ops("Reactor100"),
        "split": ops("TEE-100"),         # purge / recycle tee
        "column": ops("Twp101"),
        "flare": ops("CRV-100"),
        "recycle": ops("RCY-1"),         # convergence status of the synthesis loop
        # spreadsheet cells that fan out to several specs
        "pressure_cell": ops("MeOH Pressure").Cell("A1"),           # kPa
        "ratio_cell": ops("co2:h2_ratio_equals_3").Cell("C2"),     # H2 : CO2
        # component positions for ComponentMassFlowValue / ComponentMolarFlowValue
        "h2_index": components.index("Hydrogen"),
        "co2_index": components.index("CO2"),
        "meoh_index": components.index("Methanol"),
    }


# ---------------------------------------------------------------------------
# Running a point
# ---------------------------------------------------------------------------

def run_point(objects: dict, *, pressure=None, temperature=None, ratio=None,
              volume=None, purge_rate=None, timeout: float = 120.0) -> dict:
    """
    Set the operating point, solve, and return the results as a flat dict.

    Inputs left as None are not touched.  Pressure, temperature and volume
    accept ``(number, "unit")`` pairs; ratio and purge_rate are dimensionless.

    Sequence: freeze the solver, ignore the column and flare so the synthesis
    loop converges without them, apply the inputs, solve, restore the column
    and flare, solve again.  Both solves are awaited.

    Results are in internal units except where the key says otherwise
    (``*_kg_h`` are kg/h, ``reactor_duty_kW`` is kW).
    """
    o = objects
    units, solver = o["units"], o["solver"]
    pressure = to_internal(units, "Pressure", pressure)
    temperature = to_internal(units, "Temperature", temperature)
    volume = to_internal(units, "Volume", volume)

    t0 = time.time()
    solver.CanSolve = False
    o["column"].IsIgnored = True
    o["flare"].IsIgnored = True

    if pressure is not None:
        o["pressure_cell"].CellValue = pressure
    if temperature is not None:
        o["rin"].TemperatureValue = temperature
        o["rout"].TemperatureValue = temperature   # isothermal reactor
    if ratio is not None:
        o["ratio_cell"].CellValue = float(ratio)
    if volume is not None:
        o["reactor"].TotalVolumeValue = volume
    if purge_rate is not None:
        o["split"].SplitsValue = (float(purge_rate), 1.0 - float(purge_rate))

    solve(solver, timeout)
    o["column"].IsIgnored = False
    o["flare"].IsIgnored = False
    solve(solver, timeout)
    solver.CanSolve = True   # leave HYSYS live for interactive inspection

    co2in, h2in, meoh, purge, reactor = o["co2in"], o["h2in"], o["methanol"], o["purge"], o["reactor"]
    h2, co2, m = o["h2_index"], o["co2_index"], o["meoh_index"]
    column_ok = bool(o["column"].ColumnFlowsheet.CfsConverged)
    recycle_ok = (o["recycle"].RecycleConvergence == 1)
    return {
        "pressure_set": pressure,
        "temperature_set": temperature,
        "ratio_set": ratio,
        "volume_set": volume,
        "purge_rate_set": purge_rate,
        "pressure_actual": o["pressure_cell"].CellValue,
        "Tin": o["rin"].TemperatureValue,
        "Tout": o["rout"].TemperatureValue,
        "ratio_actual": o["ratio_cell"].CellValue,
        "volume_actual": reactor.TotalVolumeValue,
        "methanol_kg_h": meoh.MassFlowValue * 3600,
        "hydrogen_purge_kg_h": purge.ComponentMassFlowValue[h2] * 3600,
        "co2_purge_kg_h": purge.ComponentMassFlowValue[co2] * 3600,
        "co2_input_kg_h": co2in.MassFlowValue * 3600,
        "h2_input_kg_h": h2in.MassFlowValue * 3600,
        "reactor_duty_kW": reactor.HeatFlowValue,
        "reactor_dP_kPa": reactor.PressureDropValue,
        "carbon_efficiency": meoh.ComponentMolarFlowValue[m] / co2in.ComponentMolarFlowValue[co2],
        "recycle_ratio": o["recycle_gas"].MolarFlowValue / (co2in.MolarFlowValue + h2in.MolarFlowValue),
        # convergence flags: filter rows on these before trusting the numbers
        "recycle_converged": recycle_ok,
        "column_converged": column_ok,
        "recycle_iterations": int(o["recycle"].IterationsValue),
        "converged": recycle_ok and column_ok,
        "solve_time_s": round(time.time() - t0, 2),
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
