from hysyspy import HysysPy

sim = HysysPy(casename="methanol")

sim.make_parallel_copies(
    n=8,
    folder=r"C:\HYSYS_cases\\"
)