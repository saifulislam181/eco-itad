"""Eco-ITAD Flask service.

CHANGELOG (fixes):
  BUG 9 (severity: CRITICAL) -- the certificate asserted an attestation that
      never happened. The old code ran probe_capability() and then wrote
      "capability attested (block_erase=None, status=nvme-cli not found on
      PATH)" into the certificate, hashing the failure record as if it were
      evidence. The note is now derived from sanitize_safe.attested().
  BUG 10 (severity: high) -- certificates were issued from fallback telemetry.
      If smartctl was missing, H_s defaulted to 1.0 and a full-credit
      certificate was issued anyway. Unreliable telemetry now yields an
      explicitly flagged, zero-credit certificate.
  BUG 11 (severity: low) -- response headers leaked the drive serial via the
      hardware dict; the serial is no longer propagated to headers.
"""
import hashlib
import json
import time

from flask import Flask, Response, jsonify, request

import carbon
import config
import diagnostics
import ledger
import sanitize_safe

app = Flask(__name__)


@app.get("/")
def index():
    return """
    <html><body style="font-family:sans-serif;max-width:640px;margin:40px">
      <h2>Eco-ITAD Diagnostic Service</h2>
      <ul>
        <li><a href="/health">/health</a> &mdash; JSON diagnostics</li>
        <li><a href="/capability">/capability</a> &mdash; read-only sanitisation probe</li>
        <li><a href="/scan-and-certify">/scan-and-certify</a> &mdash; PDF certificate</li>
        <li><a href="/scan-and-certify?virtual=1">/scan-and-certify?virtual=1</a>
            &mdash; with virtual purge benchmark</li>
      </ul>
    </body></html>
    """


@app.get("/health")
def health():
    return jsonify(diagnostics.assess_health())


@app.get("/capability")
def capability():
    cap = sanitize_safe.probe_capability()
    cap["attested"] = sanitize_safe.attested(cap)
    return jsonify(cap)


@app.get("/scan-and-certify")
def scan_and_certify():
    """Algorithm 1 of the paper, end to end."""
    t_start = time.perf_counter()

    # --- 1. hardware assessment ------------------------------------
    hw = diagnostics.assess_health()

    # --- 2. carbon model -------------------------------------------
    t0 = time.perf_counter()
    if not hw["telemetry_reliable"]:
        # No endurance grounding -> no avoided-emission claim.
        cb = {
            "co2_saved_kg": 0.0,
            "r_life": 0.0,
            "verdict": "INDETERMINATE -> telemetry unreliable, no credit claimed",
        }
    else:
        cb = carbon.avoided_carbon(hw["score"], hw["power_on_hours"])
    hw["_timings"]["carbon_model"] = time.perf_counter() - t0

    # --- 3. sanitisation evidence (NON-DESTRUCTIVE) ----------------
    san_hash = None
    san_note = "not assessed"
    san_attested = None   # None = not performed; False = failed; True = ok

    if hw["recoverable"]:
        t0 = time.perf_counter()
        cap = sanitize_safe.probe_capability()
        hw["_timings"]["capability_probe"] = time.perf_counter() - t0

        san_hash = hashlib.sha256(
            json.dumps(cap, sort_keys=True).encode()
        ).hexdigest()
        san_attested = sanitize_safe.attested(cap)

        if san_attested:
            san_note = (f"capability attested "
                        f"(block_erase={cap['block_erase_supported']}, "
                        f"crypto_erase={cap['crypto_erase_supported']})")
        else:
            # The hash still pins the probe record, but the record is a
            # negative result and must be described as such.
            san_note = (f"capability NOT attested - probe returned no "
                        f"controller evidence (status: {cap['status']})")

        if request.args.get("virtual") == "1":
            vp = sanitize_safe.virtual_purge(size_mb=256)
            hw["_timings"]["virtual_purge"] = vp["overwrite_s"]
            san_note += (f"; virtual purge {vp['throughput_mib_s']} MiB/s on "
                         f"{vp['measured_volume']}, extrapolated "
                         f"{vp['extrapolated_full_purge_min']} min for "
                         f"{vp['target_capacity_gb']:.0f} GB (linear model)")
    elif not hw["telemetry_reliable"]:
        san_note = "not assessed - telemetry unreliable"

    # --- 4. in-memory ledger ---------------------------------------
    # NOTE: san_note / san_attested are passed through so the PDF itself
    # (the artefact an auditor actually files) states whether attestation
    # succeeded -- not only the HTTP response headers, which most readers
    # of the certificate will never see. See ledger.py BUG 15.
    t0 = time.perf_counter()
    pdf_bytes, digest = ledger.build_certificate(
        hw, cb, san_hash, sanitise_note=san_note, sanitise_attested=san_attested
    )
    hw["_timings"]["pdf_and_digest"] = time.perf_counter() - t0

    total = time.perf_counter() - t_start

    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={
            "Content-Disposition": "inline; filename=eco_itad_cert.pdf",
            "X-EcoITAD-SHA256":     digest,
            "X-EcoITAD-Score":      str(hw["score"]),
            "X-EcoITAD-Score-Degenerate": str(hw["score_is_degenerate"]),
            "X-EcoITAD-Telemetry":  hw["telemetry_source"],
            "X-EcoITAD-Reliable":   str(hw["telemetry_reliable"]),
            "X-EcoITAD-CO2e-kg":    str(cb["co2_saved_kg"]),
            "X-EcoITAD-Sanitise":   san_note,
            "X-EcoITAD-Elapsed-s":  f"{total:.3f}",
        },
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)