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

Inputs to ``run_point`` are bare numbers in the units named by ``INPUT_UNITS``
(kPa, C, m3) or ``(number, "unit")`` pairs, which are converted through
HYSYS's own unit tables.  Nothing here relies on what HYSYS uses internally:
values are written with ``SetValue(x, unit)`` and read with ``GetValue(unit)``,
and the one unitless object (the pressure spreadsheet cell) is cross-checked
against the reactor inlet pressure.  Every result key carries its unit in its name
(``T_in_C``, ``methanol_kg_h``, ``reactor_duty_kW``); dimensionless keys have
none.  Results are read from HYSYS with an explicit unit request, never via
the ``...Value`` internal-unit properties.  ``RESULT_UNITS`` maps each key to
its unit string for labelling plots and tables.
"""
from __future__ import annotations

import time
from datetime import datetime

from hysys import to_unit, solve, read, read_components

__all__ = ["cache_objects", "run_point", "INPUT_UNITS", "RESULT_UNITS"]

# Units of bare-number inputs to run_point (a (number, "unit") pair overrides them).
INPUT_UNITS = {"pressure": "kPa", "temperature": "C", "volume": "m3"}

# The pressure spreadsheet cell is a unitless number to COM.  Its unit is not
# assumed: cache_objects() and run_point() both verify that the cell value
# equals the reactor inlet pressure read explicitly in kPa.
_PRESSURE_CELL_UNIT = "kPa"
_PRESSURE_TOLERANCE = 0.005   # relative

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
    objects = {
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
    _check_pressure_cell(objects)
    return objects


def _check_pressure_cell(o: dict) -> None:
    """
    Guard against the pressure cell being in some unit other than kPa.

    The cell fans out to pressure specs; the reactor inlet stream must
    therefore read the same number when asked explicitly for kPa.
    """
    cell = float(o["pressure_cell"].CellValue)
    inlet = read(o["rin"].Pressure, _PRESSURE_CELL_UNIT)
    if abs(cell - inlet) > _PRESSURE_TOLERANCE * max(abs(inlet), 1.0):
        raise RuntimeError(
            f"pressure cell reads {cell:g} but the reactor inlet is {inlet:g} {_PRESSURE_CELL_UNIT}: "
            f"the cell's unit cannot be confirmed as {_PRESSURE_CELL_UNIT}")


# ---------------------------------------------------------------------------
# Running a point
# ---------------------------------------------------------------------------

RESULT_UNITS = {
    "pressure_set_kPa": "kPa", "temperature_set_C": "C", "ratio_set": "", "volume_set_m3": "m3",
    "purge_rate_set": "",
    "pressure_actual_kPa": "kPa", "T_in_C": "C", "T_out_C": "C", "ratio_actual": "",
    "volume_actual_m3": "m3",
    "methanol_kg_h": "kg/h", "hydrogen_purge_kg_h": "kg/h", "co2_purge_kg_h": "kg/h",
    "co2_input_kg_h": "kg/h", "h2_input_kg_h": "kg/h",
    "reactor_duty_kW": "kW", "reactor_dP_kPa": "kPa",
    "carbon_efficiency": "", "recycle_ratio_mol": "",
    "recycle_converged": "", "column_converged": "", "recycle_iterations": "", "converged": "",
    "solve_time_s": "s", "timestamp": "",
}


def run_point(objects: dict, *, pressure=None, temperature=None, ratio=None,
              volume=None, purge_rate=None, timeout: float = 120.0) -> dict:
    """
    Set the operating point, solve, and return the results as a flat dict.

    Inputs left as None are not touched.  Pressure, temperature and volume
    are bare numbers in kPa, C and m3 (``INPUT_UNITS``) or ``(number, "unit")``
    pairs; ratio and purge_rate are dimensionless.

    Sequence: freeze the solver, ignore the column and flare so the synthesis
    loop converges without them, apply the inputs, solve, restore the column
    and flare, solve again.  Both solves are awaited.

    Result keys name their unit (see ``RESULT_UNITS``).  The ``*_set_*`` keys
    echo the inputs converted to kPa, C and m3; the ``*_actual*`` keys are
    read back from HYSYS in those units: ``pressure_actual_kPa`` is the
    reactor inlet stream pressure, ``T_in_C`` / ``T_out_C`` the reactor inlet
    and outlet stream temperatures.  ``recycle_ratio_mol`` is recycle gas
    molar flow over fresh feed molar flow.

    Raises RuntimeError if, after solving, the reactor inlet pressure does not
    match the pressure that was set (the pressure cell is unitless to COM).
    """
    o = objects
    units, solver = o["units"], o["solver"]
    pressure = to_unit(units, "Pressure", pressure, INPUT_UNITS["pressure"])
    temperature = to_unit(units, "Temperature", temperature, INPUT_UNITS["temperature"])
    volume = to_unit(units, "Volume", volume, INPUT_UNITS["volume"])

    t0 = time.time()
    solver.CanSolve = False
    o["column"].IsIgnored = True
    o["flare"].IsIgnored = True

    if pressure is not None:
        o["pressure_cell"].CellValue = pressure                      # kPa, verified after the solve
    if temperature is not None:
        o["rin"].Temperature.SetValue(temperature, INPUT_UNITS["temperature"])
        o["rout"].Temperature.SetValue(temperature, INPUT_UNITS["temperature"])   # isothermal reactor
    if ratio is not None:
        o["ratio_cell"].CellValue = float(ratio)
    if volume is not None:
        o["reactor"].TotalVolume.SetValue(volume, INPUT_UNITS["volume"])
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

    _check_pressure_cell(o)
    purge_kg_h = read_components(purge.ComponentMassFlow, "kg/h")
    fresh_kgmole_h = read(co2in.MolarFlow, "kgmole/h") + read(h2in.MolarFlow, "kgmole/h")
    return {
        "pressure_set_kPa": pressure,
        "temperature_set_C": temperature,
        "ratio_set": ratio,
        "volume_set_m3": volume,
        "purge_rate_set": purge_rate,
        "pressure_actual_kPa": read(o["rin"].Pressure, "kPa"),
        "T_in_C": read(o["rin"].Temperature, "C"),
        "T_out_C": read(o["rout"].Temperature, "C"),
        "ratio_actual": float(o["ratio_cell"].CellValue),          # dimensionless spreadsheet cell
        "volume_actual_m3": read(reactor.TotalVolume, "m3"),
        "methanol_kg_h": read(meoh.MassFlow, "kg/h"),
        "hydrogen_purge_kg_h": purge_kg_h[h2],
        "co2_purge_kg_h": purge_kg_h[co2],
        "co2_input_kg_h": read(co2in.MassFlow, "kg/h"),
        "h2_input_kg_h": read(h2in.MassFlow, "kg/h"),
        "reactor_duty_kW": read(reactor.HeatFlow, "kW"),
        "reactor_dP_kPa": read(reactor.PressureDrop, "kPa"),
        "carbon_efficiency": (read_components(meoh.ComponentMolarFlow, "kgmole/h")[m]
                              / read_components(co2in.ComponentMolarFlow, "kgmole/h")[co2]),
        "recycle_ratio_mol": read(o["recycle_gas"].MolarFlow, "kgmole/h") / fresh_kgmole_h,
        # convergence flags: filter rows on these before trusting the numbers
        "recycle_converged": recycle_ok,
        "column_converged": column_ok,
        "recycle_iterations": int(o["recycle"].IterationsValue),
        "converged": recycle_ok and column_ok,
        "solve_time_s": round(time.time() - t0, 2),
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
