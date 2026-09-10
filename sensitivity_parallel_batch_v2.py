from multiprocessing import Pool
import os
import time
import itertools
from datetime import datetime
import pandas as pd

from hysys_connection import get_open_hysys_case

import pythoncom

from simulation_runner import run_case, initialise_hysys_objects

CASE_FOLDER = r"C:\HYSYS_cases"


# # --------------------------------------------------
# # Sensitivity points
# # For the first test, use 20 identical points
# # Replace later with real T/P combinations
# # --------------------------------------------------

# SENSITIVITY_POINTS = [
#     {
#         "pressure": 9000,
#         "temperature": 270,
#         "ratio": 3.0,
#         "volume": 19
#     }
#     for _ in range(20)
# ]

# # --------------------------------------------------
# # Sensitivity points
# # 
# # --------------------------------------------------
# TEMPERATURES = [260]
# PRESSURES = [8000]
TEMPERATURES = range(250, 291, 5)
PRESSURES = range(7000, 11001, 250)
RATIOS = [2.8, 3.0, 3.2]
VOLUMES = [25, 55, 85]
PURGE_RATES = [0.01, 0.05]


SENSITIVITY_POINTS = [
    {
        "pressure": p,
        "temperature": t,
        "ratio": r,
        "volume": v, 
        "purge_rate": x
    }
    for r, t, p, v, x in itertools.product(
        RATIOS,
        TEMPERATURES,    
        PRESSURES,
        VOLUMES, 
        PURGE_RATES
    )
]

# ---------------------------   -----------------------
# Worker allocation
# One HYSYS instance per worker
# --------------------------------------------------
N_Workers = 4          

## round robin allocation
chunks = [
    SENSITIVITY_POINTS[i::N_Workers]
    for i in range(N_Workers)
]

## structured allocation
# chunk_size = len(SENSITIVITY_POINTS) // N_workers

# chunks = [
#     SENSITIVITY_POINTS[i*chunk_size:(i+1)*chunk_size]
#     for i in range(N_workers - 1)
# ]

# chunks.append(
#     SENSITIVITY_POINTS[(N_workers-1)*chunk_size:]
# )

WORKERS = [
    {
        "case": os.path.join(
            CASE_FOLDER,
            f"methanol_instance{i}.hsc"
        ),
        "points": chunks[i]
    }
    for i in range(N_Workers)
]

# # --------------------------------------------------
# # One complete T/P matrix per ratio
# # --------------------------------------------------
# PRESSURES = range(7000, 11001, 125)
# TEMPERATURES = range(250, 291, 2)

# RATIOS = [3.0, 3.2, 3.4, 3.6]

# VOLUME = 19

# SENSITIVITY_POINTS = []

# for ratio in RATIOS:

#     points = [
#         {
#             "pressure": p,
#             "temperature": t,
#             "ratio": ratio,
#             "volume": VOLUME
#         }
#         for t, p in itertools.product(
#             TEMPERATURES,    
#             PRESSURES
#         )
#     ]

#     SENSITIVITY_POINTS.append(points)



# WORKERS = [
#     {
#         "case": os.path.join(
#             CASE_FOLDER,
#             f"methanol_instance{i}.hsc"
#         ),
#         "points": SENSITIVITY_POINTS[i]
#     }
#     for i in range(4)
# ]

def worker(config):

    case_path = config["case"]
    points = config["points"]

    name = os.path.basename(case_path)

    start = time.time()

    print(f"Starting {name}")


    pythoncom.CoInitialize()

    case = None

    try:

        # -----------------------------------
        # Connect to already open HYSYS case
        # -----------------------------------

        case = get_open_hysys_case(case_path)

        print(f"{name}: connected")
        print(f"{name}: case name = {case.Name}")
        print(
            f"{name}: streams = {case.Flowsheet.MaterialStreams.Count}"
        )


        print(f"{name}: caching HYSYS objects")

        objects = initialise_hysys_objects(case)
        objects["case_name"] = name
        time.sleep(1)

        print(f"{name}: objects cached")


        print(f"{name}: starting sensitivity loop")


        results = []


        for i, point in enumerate(points):

            print(
                f"{name}: sensitivity point {i+1}/{len(points)}"
            )


            result = run_case(
                objects,
                pressure=point["pressure"],
                temperature=point["temperature"],
                ratio=point["ratio"],
                volume=point["volume"], 
                purge_rate=point["purge_rate"]
            )


            # Store execution information
            result["case"] = name
            result["execution_order"] = i + 1

            # Store sensitivity variables
            result.update(point)

            results.append(result)


            print(
                f"{name}: finished point {i+1}"
            )


        elapsed = time.time() - start

        print(
            f"{name}: completed {len(results)} "
            f"points in {elapsed:.1f} s"
        )


        return results


    except Exception as e:

        print(
            f"FAILED {name}: {e}"
        )

        return [
            {
                "case": name,
                "error": str(e)
            }
        ]


    finally:

        # DO NOT close HYSYS here
        # We did not create it

        pythoncom.CoUninitialize()

        print(
            f"Finished {name}"
        )

if __name__ == "__main__":

    
    print(
        f"Total sensitivity points: "
        f"{len(SENSITIVITY_POINTS)}"
    )

    overall_start = time.time()

    with Pool(processes=N_Workers) as pool:

        results = pool.map(
            worker,
            WORKERS
        )


    print("\nFINAL RESULTS")


    total = 0
    all_results = []

    for worker_results in results:

        print("\nWorker results:")

        for r in worker_results:
            print(r)

            if "error" not in r:
                total += 1
                all_results.append(r)

    elapsed = time.time() - overall_start

    print(
        f"\nCompleted simulations: {total}"
    )

    print(
        f"Total elapsed time: {elapsed:.1f} s ({elapsed/60:.2f} min)"
    )


    # -----------------------------
    # Save results to CSV
    # -----------------------------

    if all_results:

        output_folder = "output"
        os.makedirs(output_folder, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        filename = os.path.join(
            output_folder,
            f"sensitivity_results_{timestamp}.csv"
        )

        df = pd.DataFrame(all_results)

        # df = df.sort_values(
        #     by=["pressure", "temperature", "ratio"]
        # )

        df.to_csv(
            filename,
            index=False
        )

        print(
            f"\nCSV saved to: {filename}"
        )