"""sanitize_safe.py - NON-DESTRUCTIVE sanitisation evidence collection.

CHANGELOG (fixes):
  BUG 4 (severity: high) -- a failed probe was still reportable as evidence.
      probe_capability() returned status="nvme-cli not found on PATH" with
      every flag None, and the caller embedded a SHA-256 of that record in
      the certificate labelled "capability attested". attested() now makes
      the distinction explicit and callers must check it.
  BUG 5 (severity: high) -- purge extrapolation used the wrong capacity.
      target_capacity_gb defaulted to 256 GB while the testbed drive is
      1 TB (a 4x understatement: 5.7 min vs 22.8 min). It now defaults to
      config.TARGET_CAPACITY_GB and can be auto-detected.
  BUG 6 (severity: medium) -- MiB reported as MB.
      total/(1024**2) is mebibytes. Both MiB/s and MB/s are now returned.
  BUG 7 (severity: medium) -- the scratch file lives on the OS temp drive,
      which may not be the drive being certified. The drive the benchmark
      actually measured is now recorded.
  BUG 8 (severity: low) -- purge_removable() device-name stripping broke on
      NVMe namespaces ("nvme0n1".rstrip("0123456789p") -> "nvme0n").
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone

import config


# =====================================================================
#  A.  READ-ONLY CAPABILITY PROBE       *** zero risk ***
# =====================================================================
def probe_capability(device: str | None = None) -> dict:
    """Interrogate the NVMe controller for sanitisation capability.

    READ-ONLY: issues only Identify Controller (Admin opcode 06h).
    """
    device = device or config.NVME_DEVICE
    result = {
        "device": device,
        "probed_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "method": "Identify Controller (read-only)",
        "crypto_erase_supported": None,
        "block_erase_supported": None,
        "overwrite_supported": None,
        "est_crypto_erase_s": None,
        "est_block_erase_s": None,
        "est_overwrite_s": None,
        "model": None,
        "firmware": None,
        "status": "unknown",
    }

    if shutil.which("nvme") is None:
        result["status"] = "nvme-cli not found on PATH"
        return result
    if not _looks_like_nvme(device):
        result["status"] = (f"{device} is not an NVMe namespace; "
                            "SANICAP is NVMe-only")
        return result

    try:
        proc = subprocess.run(["nvme", "id-ctrl", device, "-o", "json"],
                              capture_output=True, text=True, timeout=20)
        if proc.returncode != 0:
            result["status"] = (f"nvme id-ctrl failed (rc={proc.returncode}): "
                                f"{proc.stderr.strip()[:120]}")
            return result
        data = json.loads(proc.stdout)
        result["model"] = (data.get("mn") or "").strip()
        result["firmware"] = (data.get("fr") or "").strip()

        sanicap = int(data.get("sanicap", 0))
        result["crypto_erase_supported"] = bool(sanicap & 0x1)
        result["block_erase_supported"] = bool(sanicap & 0x2)
        result["overwrite_supported"] = bool(sanicap & 0x4)
        result["sanicap_raw"] = hex(sanicap)
        result["status"] = "ok"
    except Exception as exc:                          # noqa: BLE001
        result["status"] = f"json probe failed: {exc}"
        return result

    try:
        proc = subprocess.run(["nvme", "id-ctrl", device, "-H"],
                              capture_output=True, text=True, timeout=20)
        txt = proc.stdout
        patterns = {
            "est_crypto_erase_s": r"Estimated Time for Crypto Erase.*?(\d+)",
            "est_block_erase_s":  r"Estimated Time for Block Erase.*?(\d+)",
            "est_overwrite_s":    r"Estimated Time for Overwrite.*?(\d+)",
        }
        for key, pat in patterns.items():
            m = re.search(pat, txt, re.IGNORECASE)
            if m:
                v = int(m.group(1))
                result[key] = None if v == 0xFFFFFFFF else v
    except Exception:                                 # noqa: BLE001
        pass

    return result


def _looks_like_nvme(device: str) -> bool:
    return "nvme" in os.path.basename(device).lower()


def attested(cap: dict) -> bool:
    """True only when the controller actually returned capability flags.

    This is the guard that keeps an unsuccessful probe out of the
    certificate's attestation claim.
    """
    return cap.get("status") == "ok" and cap.get("block_erase_supported") is not None


def probe_report(device: str | None = None) -> str:
    r = probe_capability(device)
    lines = [
        "NVMe Sanitisation Capability Probe (read-only)",
        "=" * 52,
        f"Device        : {r['device']}",
        f"Model         : {r['model']}",
        f"Firmware      : {r['firmware']}",
        f"SANICAP       : {r.get('sanicap_raw', 'n/a')}",
        f"Crypto Erase  : {r['crypto_erase_supported']}",
        f"Block Erase   : {r['block_erase_supported']}",
        f"Overwrite     : {r['overwrite_supported']}",
        f"Est. crypto   : {r['est_crypto_erase_s']} s",
        f"Est. block    : {r['est_block_erase_s']} s",
        f"Est. overwrite: {r['est_overwrite_s']} s",
        f"Status        : {r['status']}",
        f"ATTESTED      : {attested(r)}   "
        f"{'' if attested(r) else '<-- NO capability evidence was obtained'}",
    ]
    return "\n".join(lines)


# =====================================================================
#  B.  FILE-BACKED VIRTUAL PURGE        *** zero risk to real data ***
# =====================================================================
def virtual_purge(size_mb: int = 512,
                  workdir: str | None = None,
                  target_capacity_gb: float | None = None) -> dict:
    """Overwrite benchmark against a temporary file-backed image."""
    if target_capacity_gb is None:
        target_capacity_gb = config.TARGET_CAPACITY_GB
    workdir = workdir or tempfile.gettempdir()
    path = os.path.join(workdir, f"_ecoitad_scratch_{secrets.token_hex(4)}.img")
    block = 4 * 1024 * 1024                 # 4 MiB write unit
    total = size_mb * 1024 * 1024
    pattern = secrets.token_bytes(block)

    stages: dict[str, float] = {}
    digest = hashlib.sha256()

    try:
        t0 = time.perf_counter()
        with open(path, "wb") as f:
            f.write(b"\x00" * block)
            f.truncate(total)
            f.flush()
            os.fsync(f.fileno())
        stages["allocate_s"] = time.perf_counter() - t0

        t0 = time.perf_counter()
        written = 0
        with open(path, "r+b") as f:
            while written < total:
                n = min(block, total - written)
                f.write(pattern[:n])
                digest.update(pattern[:n])
                written += n
            f.flush()
            os.fsync(f.fileno())
        stages["overwrite_s"] = time.perf_counter() - t0

        t0 = time.perf_counter()
        verify = hashlib.sha256()
        with open(path, "rb") as f:
            while chunk := f.read(block):
                verify.update(chunk)
        stages["verify_s"] = time.perf_counter() - t0
        verified = verify.hexdigest() == digest.hexdigest()
    finally:
        if os.path.exists(path):
            os.remove(path)

    ow = stages["overwrite_s"]
    mib_s = (total / (1024 ** 2)) / ow if ow > 0 else 0.0     # mebibytes/s
    mb_s = (total / 1_000_000) / ow if ow > 0 else 0.0        # megabytes/s
    extrapolated = (target_capacity_gb * 1024) / mib_s if mib_s else 0.0

    payload = (f"virtual-purge|{size_mb}MiB|{mib_s:.1f}MiBps|"
               f"{datetime.now(timezone.utc).isoformat()}")

    return {
        "mode": "VIRTUAL (file-backed, non-destructive)",
        "image_size_mib": size_mb,
        "allocate_s": round(stages["allocate_s"], 3),
        "overwrite_s": round(ow, 3),
        "verify_s": round(stages["verify_s"], 3),
        "throughput_mib_s": round(mib_s, 1),
        "throughput_mb_s": round(mb_s, 1),
        "verification_passed": verified,
        "extrapolated_full_purge_s": round(extrapolated, 1),
        "extrapolated_full_purge_min": round(extrapolated / 60.0, 1),
        "target_capacity_gb": target_capacity_gb,
        # The benchmark measures whichever volume hosts the temp directory,
        # which is not necessarily the drive being certified.
        "measured_volume": os.path.splitdrive(os.path.abspath(workdir))[0]
                           or os.path.abspath(workdir),
        "extrapolation_model": "linear; SLC-cache exhaustion not modelled",
        "record_hash": hashlib.sha256(payload.encode()).hexdigest(),
    }


# =====================================================================
#  C.  REMOVABLE-MEDIA PURGE            *** guarded, still dangerous ***
# =====================================================================
_TOKEN = "YES-ERASE-THIS-REMOVABLE-DEVICE"


def _sysfs_block_name(device: str) -> str:
    """Map a device node to its /sys/block entry.

    'sdb1'      -> 'sdb'
    'nvme0n1p3' -> 'nvme0n1'      (the old rstrip() produced 'nvme0n')
    """
    base = os.path.basename(device)
    m = re.match(r"(nvme\d+n\d+)(p\d+)?$", base)
    if m:
        return m.group(1)
    return base.rstrip("0123456789")


def purge_removable(device: str, confirm_token: str = "",
                    max_size_gb: float = 128.0) -> dict:
    """Destructive purge, permitted ONLY on removable media."""
    if platform.system() != "Linux":
        return {"status": "REFUSED", "reason": "Linux only"}

    base = _sysfs_block_name(device)
    try:
        if open(f"/sys/block/{base}/removable").read().strip() != "1":
            return {"status": "REFUSED",
                    "reason": f"{device} is not removable - aborting"}
        size_gb = int(open(f"/sys/block/{base}/size").read()) * 512 / (1024 ** 3)
    except Exception as exc:                          # noqa: BLE001
        return {"status": "REFUSED", "reason": f"guard check failed: {exc}"}

    if size_gb > max_size_gb:
        return {"status": "REFUSED",
                "reason": f"{size_gb:.1f} GB exceeds {max_size_gb} GB limit"}

    if confirm_token != _TOKEN:
        return {"status": "REFUSED",
                "reason": f"token required: {_TOKEN}",
                "device": device, "size_gb": round(size_gb, 1)}

    t0 = time.perf_counter()
    try:
        subprocess.run(["blkdiscard", "-f", "-s", device],
                       check=True, capture_output=True, timeout=1800)
        status, detail = "SUCCESS", "blkdiscard secure erase completed"
    except Exception as exc:                          # noqa: BLE001
        status, detail = "FAILED", str(exc)

    elapsed = time.perf_counter() - t0
    payload = f"{device}|{size_gb:.1f}GB|{status}|{elapsed:.2f}s"
    return {
        "status": status, "detail": detail, "device": device,
        "size_gb": round(size_gb, 1), "elapsed_s": round(elapsed, 2),
        # NOTE: blkdiscard -s is a discard hint, NOT a NIST SP 800-88 Purge.
        "nist_category": "Clear (discard hint) - NOT Purge",
        "record_hash": hashlib.sha256(payload.encode()).hexdigest(),
    }


if __name__ == "__main__":
    print(probe_report())
    print("\nVirtual purge benchmark (non-destructive)...")
    for k, v in virtual_purge(size_mb=256).items():
        print(f"  {k:<32}: {v}")