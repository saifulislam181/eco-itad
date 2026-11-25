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
  BUG 20 (severity: high) -- hard-coded device path did not generalise.
      config.SMART_DEVICE guessed "/dev/sda" on Windows and read_smart()
      called smartctl with no "-d <type>" hint. On a second test machine,
      `smartctl --scan` reported that /dev/sda requires an explicit
      "-d nvme" override, and only "/dev/pd0" (Windows PhysicalDrive
      numbering, exposed by some smartctl builds) returned real telemetry.
      Windows device naming for smartctl is not consistent across
      machines/drivers, so a single hard-coded guess silently degrades to
      the "fallback" (unreliable-telemetry) path on any machine where the
      guess is wrong -- with no way to tell, from the certificate alone,
      whether that happened because smartctl was absent or because the
      device string was wrong. read_smart() now calls `smartctl --scan -j`
      first and tries each reported device (with its reported -d type)
      until one returns real telemetry, instead of guessing a single path.
      UPDATE: on that same laptop, plain `smartctl -j -a /dev/sda` (no -d,
      no pd0) was later confirmed to work correctly once smartctl was
      actually installed -- the original failure was simply "smartctl not
      on PATH" at the time, not a device-path problem. The multi-candidate
      discovery logic is kept as defense-in-depth (a third machine's HDD
      needed no special handling either), but the device-path ambiguity
      itself was not, in the end, the root cause on the laptop.
  BUG 21 (severity: CRITICAL) -- the central determinism claim is false on
      unsaturated hardware. On a third test machine (an older AMD APU
      whose benchmark throughput sits close to B_REF instead of far above
      it), H_c does NOT saturate at 1.0 the way it did on the first two
      machines. With nothing clamping it, ordinary CPU-benchmark timing
      jitter (already characterised in benchmark.py's BUG 19 fix) passes
      straight through into S_health: 100 repeated assessments of the SAME
      unchanged asset produced 41 DISTINCT scores ranging 0.901-0.950 (SD
      0.011). This directly falsifies the paper's Sec. III-B claim that
      "repeated assessments of an unchanged asset... yield bitwise-
      identical certificates" -- that property only held on the first two
      machines because H_c happened to be clamped at exactly 1.0, which
      masked the underlying non-determinism rather than eliminating it.
      Fix: H_c is now computed from the MEDIAN of several internal
      benchmark repetitions (suppresses transient interference spikes,
      consistent with the BUG 19 finding) and then QUANTISED to a fixed
      step (config.COMPUTE_QUANTUM, default 0.05) before being combined
      into S_health -- restoring the "capability banding" behaviour the
      original docstring already claimed for fixed_benchmark() but that
      was never actually implemented. Small timing jitter can no longer
      change the reported score; only a difference large enough to cross
      a band boundary can. This does not make the benchmark deterministic
      in an absolute sense -- a value sitting exactly on a band edge can
      still flip -- but it bounds how often that happens, and the
      remaining edge-flip rate should be measured and reported, not
      assumed away.
"""
import json
import math
import platform
import shutil
import statistics
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


def discover_smart_devices() -> list[dict]:
    """Ask smartctl itself which devices it can see, instead of guessing a
    path. Returns a list of {"name": ..., "type": ... or None} in the order
    smartctl reports them; an empty list means the scan itself failed or
    found nothing (e.g. smartctl missing, no privileges).

    This replaces a single hard-coded config.SMART_DEVICE guess: on Windows
    in particular, whether the right device string is "/dev/sda",
    "/dev/sda" + "-d nvme", or "/dev/pd0" (PhysicalDrive numbering) varies
    by smartctl build and driver, and using the wrong one produces the same
    empty/error JSON as smartctl being entirely absent -- see BUG 20.
    """
    if shutil.which("smartctl") is None:
        return []
    try:
        proc = subprocess.run(["smartctl", "--scan", "-j"],
                              capture_output=True, text=True, timeout=15)
        data = json.loads(proc.stdout)
    except Exception:                              # noqa: BLE001
        return []
    out = []
    for dev in data.get("devices", []) or []:
        out.append({"name": dev.get("name"), "type": dev.get("type")})
    return out


def _run_smartctl(name: str, dtype: str | None) -> dict | None:
    """Run smartctl against one candidate device; return parsed JSON with a
    real device/telemetry section, or None if this candidate didn't work."""
    cmd = ["smartctl", "-j", "-a"]
    if dtype:
        cmd += ["-d", dtype]
    cmd.append(name)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        data = json.loads(proc.stdout)
    except Exception:                              # noqa: BLE001
        return None
    if not data or "device" not in data:
        return None
    return data


def read_smart(device: str | None = None) -> dict:
    """Read NVMe/SATA S.M.A.R.T. data via smartctl JSON output.

    Returns dict with keys: percentage_used, power_on_hours, model, serial,
    source ('smart-nvme' | 'smart-ata' | 'fallback'), reliable (bool),
    endurance_attribute (str|None), device_used (str|None) -- the exact
    device string that actually returned data, for auditability.

    Requires: smartmontools installed, and Administrator/root privileges.

    Device resolution order:
      1. `device` argument, if given (an explicit caller override).
      2. Every device `smartctl --scan` reports, in order, using the
         type it reports for each (BUG 20 fix -- do not hard-code a guess).
      3. config.SMART_DEVICE as a last-resort legacy fallback, kept for
         environments where --scan itself is unavailable.
    """
    candidates: list[tuple[str, str | None]] = []
    if device is not None:
        candidates.append((device, None))
    else:
        # 1) whatever `smartctl --scan` actually reports, with its type
        candidates.extend((d["name"], d["type"]) for d in discover_smart_devices()
                          if d.get("name"))
        # 2) the legacy hard-coded guess (kept for back-compat)
        candidates.append((config.SMART_DEVICE, None))
        # 3) known Windows-specific device strings that --scan has been
        #    observed NOT to report even when they are the only one that
        #    works (see BUG 20) -- tried with each common -d hint, since
        #    the right hint also varies by build/driver.
        for name in config.SMART_DEVICE_EXTRA_FALLBACKS:
            for dtype in (None, "nvme", "ata"):
                candidates.append((name, dtype))

    if shutil.which("smartctl") is None:
        return _smart_fallback("smartctl not found on PATH")

    # De-duplicate while preserving order (extra fallbacks can repeat a
    # name/type pair already produced by --scan).
    seen = set()
    deduped = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            deduped.append(c)
    candidates = deduped

    data = None
    device_used = None
    tried = []
    for name, dtype in candidates:
        tried.append(f"{name}{f' -d {dtype}' if dtype else ''}")
        result = _run_smartctl(name, dtype)
        if result is not None:
            data = result
            device_used = tried[-1]
            break

    if data is None:
        return _smart_fallback(
            "smartctl returned no usable device section on any candidate "
            f"({'; '.join(tried) if tried else 'no candidates'}) "
            "(insufficient privileges, or none of the guessed device "
            "paths matched this system)")

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
            "device_used":     device_used,
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
            "device_used":     device_used,
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
        "device_used":     device_used,
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
        "device_used":     None,
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


def robust_benchmark(repeats: int | None = None,
                     iterations: int | None = None) -> tuple[float, list[float]]:
    """Median-of-N benchmark score (see BUG 21).

    A single fixed_benchmark() call is subject to ordinary timing jitter
    and, occasionally, sharp interference spikes (BUG 19). On hardware
    whose throughput sits close to B_REF, that jitter passes straight
    through H_c into the reported S_health, breaking the paper's
    determinism claim. Taking the median of several repeats suppresses
    isolated spikes; quantising the result (see assess_health) bounds the
    remaining jitter so it cannot change the score at all unless it's
    large enough to cross a band boundary.

    Returns (median_ops_per_second, all_samples) -- the samples are kept
    on the certificate for auditability, so a reviewer can see the spread
    that was collapsed into one number rather than trusting it blindly.
    """
    repeats = repeats or config.COMPUTE_BENCH_REPEATS
    samples = [fixed_benchmark(iterations) if iterations else fixed_benchmark()
              for _ in range(repeats)]
    return statistics.median(samples), samples


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
    bench, bench_samples = robust_benchmark()
    timings["cpu_benchmark"] = time.perf_counter() - t0

    # --- sub-scores (Eq. 1) ---
    h_s_raw = 1.0 - smart["percentage_used"] / 100.0
    h_m_raw = ram_gb / config.M_REF_GB
    h_c_raw = bench / config.B_REF

    h_s = max(0.0, min(1.0, h_s_raw))
    h_m = min(1.0, h_m_raw)
    h_c_unquantised = min(1.0, h_c_raw)
    # Quantise to a fixed band so ordinary timing jitter below one band
    # width cannot change the reported score (BUG 21). This is the
    # "capability banding" behaviour fixed_benchmark()'s own docstring
    # already claimed but that was never implemented before this fix.
    q = config.COMPUTE_QUANTUM
    h_c = min(1.0, round(h_c_unquantised / q) * q)

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
        "h_compute_unquantised": round(h_c_unquantised, 4),
        "h_compute_quantum": config.COMPUTE_QUANTUM,
        "benchmark_samples": [round(s, 0) for s in bench_samples],
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
        "telemetry_device":  smart.get("device_used"),
        # An asset can only be declared recoverable if the telemetry the
        # decision rests on is itself trustworthy.
        "recoverable":      bool(score >= config.THETA_MIN
                                 and smart["reliable"]),
        "_timings":         timings,
    }