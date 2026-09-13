"""
Helpers for driving an Aspen HYSYS methanol case from Python via COM.

Typical session::

    from hysys import open_case, cache_objects, run_point, dump

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

import pythoncom
import win32com.client

__all__ = [
    "open_case", "cache_objects", "run_point",
    "to_internal", "unit_of", "members", "dump",
]


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------
def open_case(path: str, visible: bool = True):
    """
    Return the HYSYS case at `path`, attaching if it is already open.

    HYSYS registers each open case in the Windows Running Object Table under
    its file path.  If an entry matches, attach to it; otherwise start a new
    HYSYS instance and open the file.  `path` must be absolute and match the
    file name HYSYS used when opening it (comparison is case-insensitive).
    """
    pythoncom.CoInitialize()
    rot = pythoncom.GetRunningObjectTable()
    ctx = pythoncom.CreateBindCtx(0)
    enum = rot.EnumRunning()
    while (found := enum.Next(1)):
        moniker = found[0]
        try:
            if moniker.GetDisplayName(ctx, None).lower() != path.lower():
                continue
            obj = rot.GetObject(moniker).QueryInterface(pythoncom.IID_IDispatch)
            return win32com.client.Dispatch(obj)
        except pythoncom.com_error:
            continue

    print("Case not open in HYSYS - launching (this can take a minute)...")
    app = win32com.client.Dispatch("HYSYS.Application.NewInstance")
    app.Visible = visible
    return app.SimulationCases.Open(path)


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
# Units
# ---------------------------------------------------------------------------
def to_internal(units, quantity: str, value):
    """
    Convert `value` to HYSYS internal units.

    `value` is a bare number (returned unchanged, as float) or a
    ``(number, "unit")`` pair.  `quantity` names a HYSYS unit set such as
    "Pressure", "Temperature" or "Volume"; `units` is the case's
    UnitConversionSetManager (``objects["units"]``).  Unknown unit names raise.
    """
    if value is None:
        return None
    if isinstance(value, tuple):
        number, unit = value
        return units.Item(quantity).Item(unit).ToCalculationUnit(float(number))
    return float(value)  # float() also accepts numpy scalars, which COM rejects


def unit_of(units, variable) -> tuple[str, str]:
    """
    Return ``(internal_unit, display_unit)`` for a HYSYS variable object.

    `variable` is the un-suffixed property, e.g. ``stream.MassFlow`` rather
    than ``stream.MassFlowValue``.
    """
    uset = units.Item(variable.UnitConversionType)
    return uset.CalculationUnit.name, uset.CurrentDisplayUnit.name


# ---------------------------------------------------------------------------
# Running a point
# ---------------------------------------------------------------------------
def _solve(solver, timeout: float) -> None:
    """Let the solver run and block until it has finished forgetting and solving."""
    solver.CanSolve = True
    start = time.time()
    while solver.IsForgetting or solver.IsSolving:
        if time.time() - start > timeout:
            solver.CanSolve = False
            raise TimeoutError(f"HYSYS did not converge within {timeout:.0f} s")
        time.sleep(0.1)
    solver.CanSolve = False


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

    _solve(solver, timeout)
    o["column"].IsIgnored = False
    o["flare"].IsIgnored = False
    _solve(solver, timeout)
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


# ---------------------------------------------------------------------------
# Inspection
# ---------------------------------------------------------------------------
_COM_PLUMBING = {"AddRef", "Release", "QueryInterface", "GetTypeInfo",
                 "GetTypeInfoCount", "GetIDsOfNames", "Invoke"}


def members(obj, contains: str | None = None) -> dict[str, list[str]]:
    """
    List the members of a HYSYS COM object, grouped by kind.

    ``dir()`` shows nothing for late-bound COM proxies; this reads the type
    library instead.  Returns ``{"read-write": [...], "read-only": [...],
    "write-only": [...], "methods": [...]}``.  `contains` filters names
    case-insensitively.
    """
    ti = obj._oleobj_.GetTypeInfo()
    kinds: dict[str, int] = {}
    for i in range(ti.GetTypeAttr().cFuncs):
        fd = ti.GetFuncDesc(i)
        name = ti.GetNames(fd.memid)[0]
        if name.startswith("_") or name in _COM_PLUMBING:
            continue
        kinds[name] = kinds.get(name, 0) | fd.invkind

    out = {"read-write": [], "read-only": [], "write-only": [], "methods": []}
    for name, k in sorted(kinds.items()):
        if contains and contains.lower() not in name.lower():
            continue
        get = bool(k & pythoncom.INVOKE_PROPERTYGET)
        put = bool(k & (pythoncom.INVOKE_PROPERTYPUT | pythoncom.INVOKE_PROPERTYPUTREF))
        if k & pythoncom.INVOKE_FUNC and not (get or put):
            out["methods"].append(name)
        else:
            out["read-write" if get and put else "read-only" if get else "write-only"].append(name)
    return out


def _fmt(value, width: int = 60) -> str:
    """One-line rendering of a COM property value."""
    if isinstance(value, (int, float, str, bool)) or value is None:
        s = repr(value)
    elif isinstance(value, tuple):
        s = f"tuple[{len(value)}] {value[:6]!r}{' ...' if len(value) > 6 else ''}"
    else:
        try:
            s = f"<{value.Name}>"
        except Exception:
            s = "<object>"
    return s if len(s) <= width else s[: width - 4] + " ..."


def dump(obj, contains: str | None = None) -> None:
    """
    Print every readable property of a HYSYS COM object with its current value.

    ``...Value`` properties are annotated with their internal unit, read from
    the sibling variable object (``MassFlowValue`` -> ``MassFlow``).
    Properties that raise for the object's current state are shown as
    ``<error>`` rather than aborting.  Use `contains` to narrow the listing.
    """
    try:
        title = f"{obj.Name} ({obj.TypeName})"
    except Exception:
        title = "object"
    print(f"=== {title} ===")
    m = members(obj, contains)
    readable = set(members(obj)["read-write"]) | set(members(obj)["read-only"])
    try:
        units = obj.Application.UnitConversionSetManager
    except Exception:
        units = None

    def unit_for(name: str) -> str:
        base = name[:-5]
        if units is None or not name.endswith("Value") or base not in readable:
            return ""
        try:
            return units.Item(getattr(obj, base).UnitConversionType).CalculationUnit.name
        except Exception:
            return ""

    for kind in ("read-write", "read-only"):
        if m[kind]:
            print(f"\n[{kind}]")
        for name in m[kind]:
            try:
                print(f"  {name:36s} {_fmt(getattr(obj, name))} {unit_for(name)}".rstrip())
            except Exception:
                print(f"  {name:36s} <error>")
    if m["methods"]:
        print("\n[methods]")
        for name in m["methods"]:
            print(f"  {name}()")
