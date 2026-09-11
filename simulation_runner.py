import time
from datetime import datetime

def run_case(
        objects,
        pressure=None,
        temperature=None,
        ratio=None,
        purge_rate=None,
        volume=None,
):
    case_name = objects["case_name"]

    solver = objects["solver"]

    co2in = objects["co2in"]
    h2in = objects["h2in"]
    rin = objects["rin"]
    rout = objects["rout"]
    methanol = objects["methanol"]
    purge = objects["purge"]

    reactor = objects["reactor"]
    mixer = objects["mixer"]
    split = objects["split"]
    column = objects["column"]
    flare = objects["flare"]
    vap_to_mixer = objects["vap_to_mixer"]

    pressure_cell = objects["pressure_cell"]
    ratio_cell = objects["ratio_cell"]

    h2_index = objects["h2_index"]
    co2_index = objects["co2_index"]
    meoh_index = objects["meoh_index"]

    # -----------------------------
    # Change variables
    # -----------------------------

    solver.CanSolve = False

    column.IsIgnored = True
    flare.IsIgnored = True
    mixer.Feeds.Remove(1)
    solver.CanSolve = True
    start = time.time()
    while solver.IsForgetting or solver.IsSolving:

        if time.time() - start > 60:
            raise TimeoutError("HYSYS did not converge within 60 seconds")
        
        time.sleep(0.1)

    solver.CanSolve = False

    if pressure is not None:
        # t = time.time()
        pressure_cell.CellValue = pressure
        # print(f"{case_name}: pressure set in {time.time()-t:.3f}s")

    if temperature is not None:
        # t = time.time()
        rin.TemperatureValue = temperature
        rout.TemperatureValue = temperature
        # print(f"{case_name}: temperature set in {time.time()-t:.3f}s")

    if ratio is not None:
        # t = time.time()
        ratio_cell.CellValue = ratio
        # print(f"{case_name}: ratio set in {time.time()-t:.3f}s")

    if purge_rate is not None:
        split.SplitsValue = (purge_rate, 1-purge_rate)

    if volume is not None:
        reactor.TotalVolumeValue = volume


    # -----------------------------
    # Solve
    # -----------------------------
    # print(f"{case_name}: solving")

    t = time.time()

    solver.CanSolve = True

    start = time.time()

    while solver.IsForgetting or solver.IsSolving:

        if time.time() - start > 120:
            raise TimeoutError(
                "HYSYS did not converge within 120 seconds"
            )

        time.sleep(0.1)

    solver.CanSolve = False
    mixer.Feeds.Add(vap_to_mixer)    
    solver.CanSolve = True
    solver.CanSolve = False
    column.IsIgnored = False
    flare.IsIgnored = False
    solver.CanSolve = True
        
    print(f"{case_name}: solved in {time.time()-t:.2f}s")

    # -----------------------------
    # Results
    # -----------------------------

    return {

        "pressure_set": pressure,
        "temperature_set": temperature,
        "ratio_set": ratio,
        "volume_set": volume,

        "pressure_actual": pressure_cell.CellValue,
        "Tin": rin.TemperatureValue,
        "Tout": rout.TemperatureValue,
        "ratio_actual": ratio_cell.CellValue,
        "volume_actual": reactor.TotalVolumeValue,

        "methanol_kg_h": methanol.MassFlowValue * 3600,

        "hydrogen_purge_kg_h":
            purge.ComponentMassFlowValue[h2_index] * 3600,

        "co2_purge_kg_h":
            purge.ComponentMassFlowValue[co2_index]*3600,

        "co2_input_kg_h": co2in.MassFlowValue *3600,
        "h2_input_kg_h": h2in.MassFlowValue*3600,

        "reactor_duty": reactor.HeatFlowValue,

        "carbon_efficiency": methanol.ComponentMolarFlowValue[meoh_index]/co2in.ComponentMolarFlowValue[co2_index],

        "timestamp":
            datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }

def initialise_hysys_objects(case):

    print("Initialising HYSYS objects")

    flowsheet = case.Flowsheet
    streams = flowsheet.MaterialStreams
    ops = flowsheet.Operations
    solver = case.Solver

    # Material Streams
    co2in = streams("co2_1atm")
    h2in = streams("h2")
    rin = streams("RinV")
    rout = streams("RoutV")
    methanol = streams("Methanol")
    purge = streams("Purge")
    offgas = streams("flue-gas-very-cold")
    vap_to_mixer = streams("VAPToMixer")

    components = list(purge.FluidPackage.Components.Names)
    h2_index = components.index("Hydrogen")
    co2_index = components.index("CO2")
    meoh_index = components.index("Methanol")

    
    #unit operations
    reactor = ops("Reactor100")
    mixer = ops("MIX-101")
    split = ops("TEE-100")
    column = ops("Twp101")
    Flare = ops("CRV-100")

    pressure_cell = ops("MeOH Pressure").Cell("A1")
    ratio_cell = ops("co2:h2_ratio_equals_3").Cell("C2")

    return {
        "case_name": case.Name,
        "solver": solver,
        "co2in": co2in,
        "h2in": h2in,
        "rin": rin,
        "rout": rout,
        "methanol": methanol,
        "purge": purge,
        "offgas": offgas,
        "h2_index": h2_index,
        "co2_index": co2_index,
        "meoh_index": meoh_index,
        "vap_to_mixer": vap_to_mixer,
        "reactor": reactor,
        "mixer": mixer,
        "split": split,
        "column": column,
        "flare": Flare,
        "pressure_cell": pressure_cell,
        "ratio_cell": ratio_cell,
    }