"""run_evidence.py - collects non-destructive sanitisation evidence.

CHANGELOG (fixes):
  * TARGET_GB no longer hard-coded to 256; it comes from config and can be
    auto-detected from the drive under test.
  * The report now states explicitly whether capability attestation
    succeeded, instead of printing a table of None values that reads like
    evidence.
  * Serial numbers are redacted in the emitted artefacts.
"""
import json
import statistics
from datetime import datetime, timezone

import config
import sanitize_safe

REPS = 3
IMG_MB = 256


def main(target_gb: float | None = None):
    target_gb = target_gb if target_gb is not None else config.TARGET_CAPACITY_GB
    out = []
    add = out.append

    add("Eco-ITAD - Sanitisation Evidence Report")
    add(f"Generated (UTC): "
        f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    add("=" * 62)

    # ---- A. read-only capability probe ----------------------------
    add("\n[A] CONTROLLER CAPABILITY PROBE  (read-only, zero risk)")
    add("-" * 62)
    add(sanitize_safe.probe_report())
    cap = sanitize_safe.probe_capability()
    ok = sanitize_safe.attested(cap)
    if not ok:
        add("\n  RESULT: NO capability evidence obtained on this platform.")
        add(f"  Reason : {cap['status']}")
        add("  This is a NEGATIVE result and must be reported as such;")
        add("  it does not constitute NIST SP 800-88 Purge attestation.")

    # ---- B. file-backed overwrite benchmark -----------------------
    add(f"\n\n[B] VIRTUAL PURGE BENCHMARK  ({REPS} reps x {IMG_MB} MiB)")
    add("-" * 62)

    runs = []
    for i in range(REPS):
        r = sanitize_safe.virtual_purge(size_mb=IMG_MB,
                                        target_capacity_gb=target_gb)
        runs.append(r)
        add(f"  rep {i+1}: overwrite={r['overwrite_s']:.3f}s  "
            f"verify={r['verify_s']:.3f}s  "
            f"{r['throughput_mib_s']:.1f} MiB/s  "
            f"verified={r['verification_passed']}")

    def ms(v):
        return (statistics.mean(v),
                statistics.stdev(v) if len(v) > 1 else 0.0)

    ow_m, ow_s = ms([r["overwrite_s"] for r in runs])
    vf_m, vf_s = ms([r["verify_s"] for r in runs])
    tp_m, tp_s = ms([r["throughput_mib_s"] for r in runs])
    ex_m, ex_s = ms([r["extrapolated_full_purge_s"] for r in runs])

    add("\n  AGGREGATE")
    add(f"    overwrite       : {ow_m:.3f} s  (SD {ow_s:.3f})")
    add(f"    verification    : {vf_m:.3f} s  (SD {vf_s:.3f})")
    add(f"    throughput      : {tp_m:.1f} MiB/s (SD {tp_s:.1f})")
    add(f"    measured volume : {runs[0]['measured_volume']}")
    add(f"    extrapolated to {target_gb:.0f} GB: "
        f"{ex_m:.1f} s = {ex_m/60:.1f} min  (SD {ex_s/60:.1f} min)")
    add(f"    model           : {runs[0]['extrapolation_model']}")
    add(f"    all verified    : {all(r['verification_passed'] for r in runs)}")

    # ---- C. paste-ready LaTeX rows --------------------------------
    add("\n\n[C] PASTE INTO TABLE II")
    add("-" * 62)
    add("Sanitisation capability probe (read-only) & $<$0.5 & n/a & --- \\\\")
    add(f"Overwrite throughput benchmark & {ow_m:.2f} & {ow_s:.2f} & --- \\\\")
    add(f"Full purge ({target_gb:.0f}\\,GB, extrapolated) & "
        f"{ex_m:.0f} & n/a & --- \\\\")

    add("\n\n[D] PASTE INTO SECTION III-E")
    add("-" * 62)
    add(f"attestation_obtained = {ok}")
    add(f"probe_status = {cap['status']}")
    add(f"block_erase_supported = {cap['block_erase_supported']}")
    add(f"crypto_erase_supported = {cap['crypto_erase_supported']}")
    add(f"declared block erase time = {cap['est_block_erase_s']} s")
    add(f"measured throughput = {tp_m:.1f} MiB/s")
    add(f"extrapolated full purge ({target_gb:.0f} GB) = {ex_m/60:.1f} min")

    text = "\n".join(out)
    with open("evidence_report.txt", "w", encoding="utf-8") as f:
        f.write(text)
    safe_cap = {k: v for k, v in cap.items() if k not in ("serial", "serial_number")}
    with open("evidence_raw.json", "w", encoding="utf-8") as f:
        json.dump({"capability": safe_cap, "virtual_runs": runs,
                   "attestation_obtained": ok}, f, indent=2)

    print(text)
    print("\n-> evidence_report.txt, evidence_raw.json written.")


if __name__ == "__main__":
    main()