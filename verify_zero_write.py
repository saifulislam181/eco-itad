"""
verify_zero_write.py — isolates disk-write attribution.

Compares three conditions with NO printing during measurement:
  (a) idle control            -> interpreter baseline
  (b) certificate generation  -> the claim under test
  (c) explicit file write     -> positive control (sanity check)
"""
import os
import tempfile

import psutil

import carbon
import diagnostics
import ledger

proc = psutil.Process()
REPS = 20


def delta(fn):
    """Measure write_bytes attributable to fn(), silently."""
    before = proc.io_counters().write_bytes
    for _ in range(REPS):
        fn()
    return proc.io_counters().write_bytes - before


# --- (a) idle control ---------------------------------------------
def _idle():
    _ = sum(range(1000))


# --- (b) the actual claim -----------------------------------------
_hw = diagnostics.assess_health()
_cb = carbon.avoided_carbon(_hw["score"], _hw["power_on_hours"])


def _certify():
    ledger.build_certificate(_hw, _cb, None)


# --- (c) positive control -----------------------------------------
def _write_file():
    p = os.path.join(tempfile.gettempdir(), "_ctrl.bin")
    with open(p, "wb") as f:
        f.write(b"x" * 1024)
        f.flush()
        os.fsync(f.fileno())
    os.remove(p)


a = delta(_idle)
b = delta(_certify)
c = delta(_write_file)

print(f"""
Disk-write attribution ({REPS} repetitions each)
================================================
(a) idle control            : {a:>8} bytes
(b) certificate generation  : {b:>8} bytes   <-- the claim
(c) explicit 1 KiB write    : {c:>8} bytes   <-- positive control

Certificate writes attributable above baseline: {max(0, b - a)} bytes
Positive control confirms measurement works   : {c > 0}
""")
