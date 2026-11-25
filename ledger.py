"""Zero-disk-footprint compliance ledger generation.

CHANGELOG (fixes):
  BUG 15 (severity: CRITICAL, continuation of app.py C1) -- the certificate
      itself never received the attestation outcome. app.py computed a
      human-readable `san_note` distinguishing "attested" from "NOT
      attested", but build_certificate() only accepted a bare hash and
      printed either that hash or "not performed". An auditor who only
      ever sees the PDF (the actual artefact, not the HTTP response
      headers) had no way to tell a successful attestation from a failed
      probe -- both rendered as "a hash is present". The signature now
      takes the note and an explicit `attested` flag, and prints one of
      three distinct states: NOT PERFORMED / NOT ATTESTED (probe failed) /
      ATTESTED.
  BUG 16 (severity: high) -- the section heading asserted a Purge occurred.
      "Media Sanitisation (NIST SP 800-88 Rev.1 Purge)" is printed on every
      certificate regardless of outcome, including when the probe found no
      capability evidence at all. Retitled to a neutral "Media Sanitisation
      Evidence" with the actual mechanism (or its absence) stated below it.
  BUG 17 (severity: medium) -- degenerate score and telemetry reliability
      were computed by diagnostics.assess_health() but never surfaced on
      the certificate itself, only in the JSON API. Since the PDF is the
      artefact that gets filed for compliance, both are now disclosed
      in-document rather than only over the API.
  BUG 18 (severity: low) -- no certificate identifier. Two certificates for
      the same asset on the same day were indistinguishable without
      opening both and diffing the hash. A short id (first 12 hex chars of
      a content hash minus the timestamp) is now printed.
"""
import hashlib
import io
from datetime import datetime, timezone

from reportlab.lib.pagesizes import letter
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

import config


def build_certificate(hw: dict, carbon: dict,
                      sanitise_hash: str | None = None,
                      sanitise_note: str | None = None,
                      sanitise_attested: bool | None = None) -> tuple[bytes, str]:
    """Render the Scope 3 certificate entirely in RAM.

    sanitise_hash     : SHA-256 of the probe/benchmark record, if any probe ran.
    sanitise_note     : human-readable outcome, e.g. "capability attested
                        (block_erase=True)" or "capability NOT attested -
                        probe returned no controller evidence". This is the
                        text that ends up on the certificate; passing only
                        the hash (as earlier versions did) hides the
                        attested/not-attested distinction from anyone who
                        reads the PDF instead of the HTTP headers.
    sanitise_attested : explicit tri-state signal so the certificate can be
                        unambiguous even if `sanitise_note` is missing:
                        None -> not performed, False -> attempted and
                        failed, True -> succeeded.

    Returns (pdf_bytes, sha256_hexdigest). No file is created on host
    storage at any point.
    """
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    w, h = letter
    y = h - 25 * mm

    def line(text, size=10, dy=6 * mm, bold=False, color=None):
        nonlocal y
        c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        if color:
            c.setFillColorRGB(*color)
        c.drawString(20 * mm, y, text)
        if color:
            c.setFillColorRGB(0, 0, 0)
        y -= dy

    issued = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cert_id = hashlib.sha256(
        f"{issued}|{hw.get('score')}|{hw.get('power_on_hours')}".encode()
    ).hexdigest()[:12]

    line("Eco-ITAD \u2014 Scope 3 Compliance Certificate", 15, 10 * mm, True)
    line(f"Certificate ID : {cert_id}")
    line(f"Issued (UTC)   : {issued}")
    y -= 3 * mm

    line("Asset Metadata", 12, 7 * mm, True)
    line(f"Processor      : {hw['processor']}")
    line(f"Operating Sys. : {hw['os']}")
    line(f"Installed RAM  : {hw['ram_gb']} GB")
    line(f"Storage Model  : {hw['drive_model']}")
    line(f"Telemetry Src. : {hw['telemetry_source']}")
    if hw.get("telemetry_device"):
        line(f"Telemetry Dev. : {hw['telemetry_device']}", 8)
    if not hw.get("telemetry_reliable", True):
        line("*** TELEMETRY UNRELIABLE -- no endurance grounding ***",
             10, bold=True, color=(0.7, 0, 0))
    y -= 3 * mm

    line("Health Assessment (Eq. 1)", 12, 7 * mm, True)
    line(f"H_storage (w={config.W_STORAGE}) : {hw['h_storage']}   "
         f"[SMART used: {hw['percentage_used']}%, "
         f"attr: {hw.get('endurance_attribute') or 'n/a'}]")
    line(f"H_memory  (w={config.W_MEMORY}) : {hw['h_memory']}"
         + ("  [SATURATED at 1.0]" if "memory" in hw.get("saturated_terms", []) else ""))
    line(f"H_compute (w={config.W_COMPUTE}) : {hw['h_compute']}"
         + ("  [SATURATED at 1.0]" if "compute" in hw.get("saturated_terms", []) else ""))
    if hw.get("h_compute_quantum") and not hw.get("score_is_degenerate", False):
        line(f"  (quantised to {hw['h_compute_quantum']} bands; "
             f"raw median {hw.get('h_compute_unquantised', hw['h_compute'])}, "
             f"samples {hw.get('benchmark_samples', [])})", 7)
    line(f"S_health              : {hw['score']}", bold=True)
    if hw.get("score_is_degenerate"):
        line("NOTE: one or more sub-scores saturated; S_health does not "
             "vary with the saturated dimension(s) on this asset.", 8)
    line(f"Power-on hours        : {hw['power_on_hours']:.0f}")
    y -= 3 * mm

    line("Scope 3 Carbon Allocation (Eq. 2-3)", 12, 7 * mm, True)
    line(f"C_embodied  : {config.C_EMBODIED} kg CO2e")
    line(f"R_life      : {carbon['r_life']}")
    line(f"gamma_rec   : {config.GAMMA_REC}")
    line(f"C_saved     : {carbon['co2_saved_kg']} kg CO2e", 12, bold=True)
    line(f"Verdict     : {carbon['verdict']}")
    y -= 3 * mm

    line("Media Sanitisation Evidence", 12, 7 * mm, True)
    if sanitise_attested is True:
        status_line = "STATUS: ATTESTED (controller capability confirmed)"
    elif sanitise_attested is False:
        status_line = "STATUS: NOT ATTESTED (probe attempted, no evidence obtained)"
    elif sanitise_hash:
        status_line = "STATUS: probe ran, outcome not classified"
    else:
        status_line = "STATUS: NOT PERFORMED"
    line(status_line, 10, bold=(sanitise_attested is not True))
    if sanitise_note:
        line(sanitise_note, 8)
    line(f"Record hash : {sanitise_hash or 'n/a'}", 8)

    c.setFont("Helvetica-Oblique", 7)
    c.drawString(20 * mm, 15 * mm,
                 "Generated in volatile memory. Self-issued attestation, "
                 "not third-party verified. See Eco-ITAD paper Sec. IV-E "
                 "for scope and limitations of the zero-write claim.")
    c.showPage()
    c.save()

    pdf_bytes = buf.getvalue()
    digest = hashlib.sha256(pdf_bytes).hexdigest()
    buf.close()
    return pdf_bytes, digest