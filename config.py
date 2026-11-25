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
COMPUTE_BENCH_REPEATS = 3      # median-of-N internal repeats for H_c (BUG 21):
                               # suppresses transient timing spikes (BUG 19)
                               # before the value is quantised below.
COMPUTE_QUANTUM = 0.05         # H_c is rounded to this step (BUG 21) so that
                               # ordinary timing jitter smaller than one band
                               # cannot change the reported S_health. Smaller
                               # = more sensitive to real hardware
                               # differences but less robust to jitter;
                               # larger = the reverse. 0.05 gives 21 bands
                               # over [0,1].

# --- Device / capacity ---
# smartctl device string. Kept only as the LAST-RESORT legacy candidate;
# diagnostics.read_smart() now asks `smartctl --scan` for the real device
# list first (see BUG 20). "/dev/sda" is the common Windows convention, but
# is not universal across smartctl builds/drivers.
SMART_DEVICE = "/dev/sda" if platform.system() == "Windows" else "/dev/nvme0"
# Extra Windows-specific fallback candidates, tried after --scan and after
# SMART_DEVICE, because `smartctl --scan` was observed NOT to list these
# even when they were the only device string that actually worked on a
# real Windows test machine (PhysicalDrive numbering exposed by some
# smartctl/driver combinations as "/dev/pd0", "/dev/pd1", ...).
SMART_DEVICE_EXTRA_FALLBACKS = (
    ["/dev/pd0", "/dev/pd1"] if platform.system() == "Windows" else []
)
NVME_DEVICE  = "/dev/nvme0"     # used only by the NVMe capability probe
TARGET_CAPACITY_GB = 1024.0     # capacity of the drive under test (was 256.0)

assert abs(W_STORAGE + W_MEMORY + W_COMPUTE - 1.0) < 1e-9