"""
PFR in isolation: the methanol reactor with its inlet and outlet streams only.
Generic HYSYS helpers (connection, units, inspection) live in ``hysys.py``.

Typical session::

    from hysys import open_case, dump
    from hysys_pfr import cache_objects, run_point

    case    = open_case(r"C:\\...\\PFR.hsc")
    objects = cache_objects(case)
    row     = run_point(objects, pressure=(90, "bar"), temperature=(259, "C"), volume=(19, "m3"),
                        Hydrogen=2.25, CO=0.059, CO2=0.50, H2O=0.008, Methanol=0.004, Nitrogen=0.045)

Component flows are inlet molar flows in kgmole/s (HYSYS internal), or
``(number, "unit")`` pairs such as ``(8100, "kgmole/h")``.  Components not
named are set to zero.  The reactor runs isothermally: the outlet temperature
is set equal to the inlet and the duty is a result.
"""
from __future__ import annotations

import time
from datetime import datetime

from hysys import to_internal, solve, by_component

__all__ = ["cache_objects", "run_point"]


# ---------------------------------------------------------------------------
# Flowsheet objects
# ---------------------------------------------------------------------------
def cache_objects(case) -> dict:
    """
    Look up the streams and unit operations used by `run_point` once.

    Each attribute access on a COM proxy is a round trip into HYSYS, so
    callers keep this dict for the life of the session.
    """
    fs = case.Flowsheet
    rin = fs.MaterialStreams("RinV")
    return {
        "solver": case.Solver,
        "units": case.Application.UnitConversionSetManager,
        "rin": rin,                                  # reactor inlet: fully specified per point
        "rout": fs.MaterialStreams("RoutV"),         # reactor outlet
        "reactor": fs.Operations("Reactor100"),
        "components": list(rin.FluidPackage.Components.Names),   # order of Component...Value tuples
    }


# ---------------------------------------------------------------------------
# Running a point
# ---------------------------------------------------------------------------
def run_point(objects: dict, *, pressure, temperature, volume,
              timeout: float = 120.0, **flows) -> dict:
    """
    Set inlet conditions, composition and reactor volume, solve, return a flat dict.

    `pressure`, `temperature`, `volume` accept bare numbers (kPa, C, m3) or
    ``(number, "unit")`` pairs.  `flows` are inlet molar flows keyed by HYSYS
    component name (kgmole/s or ``(number, "unit")``); unnamed components are
    zero.  Outlet flows are returned per component as ``out_<name>`` in
    kgmole/s, alongside CO2 conversion, duty and pressure drop.
    """
    o = objects
    units, solver, rin, rout, reactor = o["units"], o["solver"], o["rin"], o["rout"], o["reactor"]
    pressure = to_internal(units, "Pressure", pressure)
    temperature = to_internal(units, "Temperature", temperature)
    volume = to_internal(units, "Volume", volume)

    unknown = set(flows) - set(o["components"])
    if unknown:
        raise KeyError(f"not in fluid package: {sorted(unknown)}; valid: {o['components']}")
    inlet = tuple(to_internal(units, "Molar Flow", flows.get(c, 0.0)) for c in o["components"])

    t0 = time.time()
    solver.CanSolve = False
    rin.PressureValue = pressure
    rin.TemperatureValue = temperature
    rout.TemperatureValue = temperature          # isothermal reactor, duty is the result
    rin.ComponentMolarFlowValue = inlet          # sets composition and total flow together
    reactor.TotalVolumeValue = volume
    solve(solver, timeout)
    solver.CanSolve = True                       # leave HYSYS live for interactive inspection

    converged = bool(rout.MolarFlow.IsKnown and rout.Temperature.IsKnown)
    out = by_component(rout, "ComponentMolarFlowValue") if converged else {}
    inlet_by_name = dict(zip(o["components"], inlet))
    row = {
        "pressure_set": pressure,
        "temperature_set": temperature,
        "volume_set": volume,
        **{f"in_{c}": v for c, v in inlet_by_name.items()},
        "inlet_kgmole_s": sum(inlet),
        **{f"out_{c}": v for c, v in out.items()},
        "co2_conversion": 1 - out["CO2"] / inlet_by_name["CO2"] if converged and inlet_by_name["CO2"] else None,
        "reactor_duty_kW": reactor.HeatFlowValue if converged else None,
        "reactor_dP_kPa": reactor.PressureDropValue if converged else None,
        "converged": converged,
        "solve_time_s": round(time.time() - t0, 2),
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    return row