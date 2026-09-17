"""Telemetry acquisition and deterministic composite health scoring.

Implements Eq. (1) of the paper.

CHANGELOG (fixes):
  BUG 1 (severity: high) -- SATA attribute selection.
      The old loop accepted attribute IDs (231, 233, 177) by number only and
      broke on the first match. ID 231 is "Temperature" on most vendors and
      "SSD_Life_Left" only on SandForce-based drives; a temperature reading of
      70 would silently become "30% endurance used". Selection is now done by
      attribute *name*, temperature attributes are explicitly excluded, and
      the attribute actually used is reported for auditability.
  BUG 2 (severity: high) -- the "conservative" fallback was optimistic.
      _smart_fallback() returned percentage_used = 0.0, i.e. H_s = 1.0, the
      best possible storage score, whenever smartctl was missing or the run
      lacked privileges. It now flags the record as unreliable and the
      caller must refuse to issue an endurance-grounded certificate.
  BUG 3 (severity: medium) -- saturation was invisible.
      H_m and H_c clamp at 1.0. On the evaluation platform both saturate,
      so the "composite" score is a function of H_s alone. The clamp state
      is now reported so it can be disclosed in the paper.
"""
import json
import math
import platform
import shutil
import subprocess
import time

import psutil

import config

# Attribute names that carry endurance information, normalised-value semantics:
# value counts DOWN from 100 as the drive wears, so used% = 100 - value.
_ENDURANCE_NAME_HINTS = (
    "wear_leveling", "wear_levelling", "ssd_life_left", "percent_lifetime",
    "percent_life", "media_wearout", "remaining_life", "lifetime_remain",
)
# Never interpret these as endurance, whatever their ID.
_EXCLUDE_NAME_HINTS = ("temperature", "airflow", "temp_")


def read_smart(device: str | None = None) -> dict:
    """Read NVMe/SATA S.M.A.R.T. data via smartctl JSON output.

    Returns dict with keys: percentage_used, power_on_hours, model, serial,
    source ('smart-nvme' | 'smart-ata' | 'fallback'), reliable (bool),
    endurance_attribute (str|None).

    Requires: smartmontools installed, and Administrator/root privileges.
    """
    if device is None:
        device = config.SMART_DEVICE

    if shutil.which("smartctl") is None:
        return _smart_fallback("smartctl not found on PATH")

    try:
        proc = subprocess.run(
            ["smartctl", "-j", "-a", device],
            capture_output=True, text=True, timeout=30,
        )
        data = json.loads(proc.stdout)
    except Exception as exc:                      # noqa: BLE001
        return _smart_fallback(f"smartctl invocation failed: {exc}")

    # smartctl sets bit 0/1 of exit status for command-line / device-open errors
    if not data or "device" not in data:
        return _smart_fallback("smartctl returned no device section "
                               "(insufficient privileges?)")

    # --- NVMe path -----------------------------------------------------
    nvme = data.get("nvme_smart_health_information_log")
    if nvme:
        return {
            "percentage_used": float(nvme.get("percentage_used", 0)),
            "power_on_hours":  float(nvme.get("power_on_hours", 0)),
            "media_errors":    int(nvme.get("media_errors", 0)),
            "model":           data.get("model_name", "unknown"),
            "serial":          data.get("serial_number", "unknown"),
            "source":          "smart-nvme",
            "reliable":        True,
            "endurance_attribute": "nvme:percentage_used",
        }

    # --- SATA SSD/HDD path ---------------------------------------------
    hours = data.get("power_on_time", {}).get("hours", 0)
    pct_used, used_attr = None, None

    for attr in data.get("ata_smart_attributes", {}).get("table", []):
        name = str(attr.get("name", "")).lower()
        if any(h in name for h in _EXCLUDE_NAME_HINTS):
            continue
        if not any(h in name for h in _ENDURANCE_NAME_HINTS):
            continue
        norm = attr.get("value")
        if norm is None:
            continue
        norm = float(norm)
        if not 0.0 <= norm <= 100.0:      # normalised values live in [0,100]
            continue
        pct_used = max(0.0, 100.0 - norm)
        used_attr = f"ata:{attr.get('id')}:{attr.get('name')}"
        break

    if pct_used is None:
        # No endurance attribute at all (typical for HDDs). Do NOT silently
        # assume a pristine drive -- flag it instead.
        return {
            "percentage_used": 0.0,
            "power_on_hours":  float(hours),
            "media_errors":    0,
            "model":           data.get("model_name", "unknown"),
            "serial":          data.get("serial_number", "unknown"),
            "source":          "smart-ata",
            "reliable":        False,
            "endurance_attribute": None,
        }

    return {
        "percentage_used": pct_used,
        "power_on_hours":  float(hours),
        "media_errors":    0,
        "model":           data.get("model_name", "unknown"),
        "serial":          data.get("serial_number", "unknown"),
        "source":          "smart-ata",
        "reliable":        True,
        "endurance_attribute": used_attr,
    }


def _smart_fallback(reason: str) -> dict:
    """Fallback when SMART is unavailable.

    NOTE: percentage_used = 0.0 is the *most favourable* value, so this
    record is explicitly marked unreliable. A certificate produced from an
    unreliable record is not endurance-grounded and must be refused or
    clearly flagged (see app.py).
    """
    return {
        "percentage_used": 0.0,
        "power_on_hours":  0.0,
        "media_errors":    0,
        "model":           "unavailable",
        "serial":          "unavailable",
        "source":          f"fallback ({reason})",
        "reliable":        False,
        "endurance_attribute": None,
    }


# ----------------------------------------------------------------------
# 2. Fixed-workload CPU benchmark  ->  H_c
# ----------------------------------------------------------------------
def fixed_benchmark(iterations: int = config.BENCH_ITERATIONS) -> float:
    """Deterministic integer/float workload. Returns operations per second."""
    start = time.perf_counter()
    acc = 0.0
    for i in range(1, iterations + 1):
        acc += math.sqrt(i) * 1.0000001
    elapsed = time.perf_counter() - start
    return iterations / elapsed if elapsed > 0 else 0.0


def calibrate_benchmark() -> float:
    """Run once on a known-good REFERENCE node (not the node under test)."""
    scores = [fixed_benchmark() for _ in range(5)]
    ref = sum(scores) / len(scores)
    print(f"[calibration] B_REF = {ref:.0f}  (samples: "
          f"{[f'{s:.0f}' for s in scores]})")
    return ref


# ----------------------------------------------------------------------
# 3. Composite health score  (Eq. 1)
# ----------------------------------------------------------------------
def assess_health(device: str | None = None) -> dict:
    """Full assessment. Returns all sub-scores and clamp state."""
    timings = {}

    t0 = time.perf_counter()
    smart = read_smart(device)
    timings["smart_poll"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    ram_gb = psutil.virtual_memory().total / (1024 ** 3)
    timings["memory_enum"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    bench = fixed_benchmark()
    timings["cpu_benchmark"] = time.perf_counter() - t0

    # --- sub-scores (Eq. 1) ---
    h_s_raw = 1.0 - smart["percentage_used"] / 100.0
    h_m_raw = ram_gb / config.M_REF_GB
    h_c_raw = bench / config.B_REF

    h_s = max(0.0, min(1.0, h_s_raw))
    h_m = min(1.0, h_m_raw)
    h_c = min(1.0, h_c_raw)

    score = round(
        config.W_STORAGE * h_s
        + config.W_MEMORY * h_m
        + config.W_COMPUTE * h_c,
        3,
    )

    # Saturation disclosure: when a term clamps, it carries no information
    # and the composite score degenerates to the remaining terms.
    saturated = [n for n, raw in (("memory", h_m_raw), ("compute", h_c_raw))
                 if raw >= 1.0]

    return {
        "score":            score,
        "h_storage":        round(h_s, 3),
        "h_memory":         round(h_m, 3),
        "h_compute":        round(h_c, 3),
        "h_memory_raw":     round(h_m_raw, 3),
        "h_compute_raw":    round(h_c_raw, 3),
        "saturated_terms":  saturated,
        "score_is_degenerate": len(saturated) > 0,
        "power_on_hours":   smart["power_on_hours"],
        "percentage_used":  smart["percentage_used"],
        "endurance_attribute": smart["endurance_attribute"],
        "telemetry_reliable":  smart["reliable"],
        "media_errors":     smart["media_errors"],
        "ram_gb":           round(ram_gb, 1),
        "benchmark_ops":    round(bench, 0),
        "b_ref":            config.B_REF,
        "processor":        platform.processor() or platform.machine(),
        "os":               f"{platform.system()} {platform.release()}",
        "drive_model":      smart["model"],
        "telemetry_source": smart["source"],
        # An asset can only be declared recoverable if the telemetry the
        # decision rests on is itself trustworthy.
        "recoverable":      bool(score >= config.THETA_MIN
                                 and smart["reliable"]),
        "_timings":         timings,
    }