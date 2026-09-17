"""Eco-ITAD - central configuration.
All constants here map 1:1 to the symbols in the paper.

CHANGELOG (fixes):
  * B_REF is now an explicitly documented, platform-independent baseline.
    The previous value (6e6) was the un-calibrated placeholder while the
    paper claimed it was "calibrated on the evaluation platform".
  * TARGET_CAPACITY_GB added: the purge extrapolation used a hard-coded
    256 GB while the testbed drive is 1 TB.
  * SMART_DEVICE / NVME_DEVICE are configurable instead of hard-coded.
"""
import platform

# --- Carbon model (Eq. 2-3) ---
C_EMBODIED   = 320.0     # kg CO2e, desktop manufacturing baseline  [ref10]
GAMMA_REC    = 0.6875    # recovery efficiency coefficient (tunable, see Sec. IV-D)
U_MAX        = 43_800.0  # hours = 5-year operational lifecycle
THETA_MIN    = 0.50      # non-recoverable gate (NOT a credit floor)

# --- Health score weights (Eq. 1), must sum to 1.0 ---
W_STORAGE    = 0.4
W_MEMORY     = 0.3
W_COMPUTE    = 0.3

# --- Reference baselines ---
M_REF_GB     = 16.0        # reference enterprise RAM baseline
# B_REF: ops/s of the fixed workload on the *reference* node.
# IMPORTANT: this must be a fixed, documented baseline shared across the
# estate -- NOT the value measured on the machine under test, otherwise
# H_c is 1.0 by construction and contributes no information.
# Measured on the evaluation platform (AMD Zen+): 11,260,330 ops/s.
# Declared enterprise baseline (2019-class desktop CPU):
B_REF        = 8_000_000
B_REF_SOURCE = ("declared enterprise baseline; evaluation platform measured "
                "11_260_330 ops/s -> H_c saturates, see Sec. IV-B")

# --- Benchmark harness ---
BENCH_ITERATIONS = 3_000_000   # fixed deterministic workload size
BENCH_WARMUP_RUNS = 1          # discarded cold-start runs (interpreter warm-up)

# --- Device / capacity ---
# smartctl device string. On Windows, smartctl maps physical disk 0 to /dev/sda.
SMART_DEVICE = "/dev/sda" if platform.system() == "Windows" else "/dev/nvme0"
NVME_DEVICE  = "/dev/nvme0"     # used only by the NVMe capability probe
TARGET_CAPACITY_GB = 1024.0     # capacity of the drive under test (was 256.0)

assert abs(W_STORAGE + W_MEMORY + W_COMPUTE - 1.0) < 1e-9