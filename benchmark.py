"""Benchmark harness -- produces every number and figure the paper needs.

Usage:
    python benchmark.py --runs 50

CHANGELOG (fixes):
  BUG 12 (severity: high) -- the zero-write measurement was contaminated.
      io_counters() was sampled around a loop that print()s once per run.
      On Windows, console writes are counted in WriteTransferCount, so the
      "402 bytes" cited in the paper as certificate-pipeline overhead
      partly measures stdout. Per-run output is now buffered and printed
      only after io_after is taken.
  BUG 13 (severity: medium) -- peak CPU > 100% was mislabelled.
      psutil.Process.cpu_percent(None) normalises to one core and can
      exceed 100% purely as a sampling artefact of the interval-free mode.
      It is NOT Turbo Boost. Output now says so, and a bounded-interval
      sample is used.
  BUG 14 (severity: medium) -- cold-start run inflated the reported SD.
      Run 1 is an interpreter/JIT warm-up outlier (z = 11 in the shipped
      data). Both all-runs and steady-state statistics are now reported.
  BUG 19 (severity: medium) -- longer sessions (n=100) showed sporadic
      high-latency runs (z up to 15) that are NOT explained by the
      cold-start rule above: they cluster in the second half of the run
      and live CPU-frequency monitoring during a repeat session showed no
      frequency drop, which rules out thermal throttling. The likely cause
      is intermittent OS-level scheduling interference (e.g. background
      network/telemetry activity). Because a handful of large spikes can
      dominate a plain mean/SD, mean/SD alone is no longer trustworthy at
      n=100. The harness now also reports the median, IQR, and any run
      beyond the Tukey 1.5x IQR fence, and a trimmed mean/SD with those
      runs excluded, so the paper can report both honestly instead of
      silently dropping outliers or letting them inflate SD unexplained.
"""
import argparse
import csv
import statistics
import threading
import time

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import psutil

import carbon
import config
import diagnostics
import ledger

STAGES = ["smart_poll", "memory_enum", "cpu_benchmark",
          "carbon_model", "pdf_and_digest"]
LABELS = {
    "smart_poll":     "S.M.A.R.T. telemetry poll",
    "memory_enum":    "Memory topology enumeration",
    "cpu_benchmark":  "Fixed-workload CPU benchmark",
    "carbon_model":   "Carbon model evaluation",
    "pdf_and_digest": "PDF render + SHA-256 digest",
}


class ResourceMonitor(threading.Thread):
    """Samples host CPU% and RSS at 100 ms intervals."""

    def __init__(self):
        super().__init__(daemon=True)
        self.proc = psutil.Process()
        self.samples: list[tuple[float, float, float]] = []
        self._stop = threading.Event()

    def run(self):
        t0 = time.perf_counter()
        self.proc.cpu_percent(None)  # prime
        while not self._stop.is_set():
            # Bounded interval: cpu_percent(None) divides by the elapsed
            # wall time since the previous call, which under scheduling
            # jitter can yield values above 100% of one core.
            cpu = self.proc.cpu_percent(interval=0.1)
            self.samples.append((
                time.perf_counter() - t0,
                cpu,
                self.proc.memory_info().rss / (1024 ** 2),
            ))

    def stop(self):
        self._stop.set()
        self.join(timeout=2)


def single_run() -> dict:
    t_start = time.perf_counter()
    hw = diagnostics.assess_health()
    t0 = time.perf_counter()
    cb = carbon.avoided_carbon(hw["score"], hw["power_on_hours"])
    hw["_timings"]["carbon_model"] = time.perf_counter() - t0
    t0 = time.perf_counter()
    ledger.build_certificate(hw, cb, None)
    hw["_timings"]["pdf_and_digest"] = time.perf_counter() - t0
    hw["_timings"]["total"] = time.perf_counter() - t_start
    hw["_timings"]["score"] = hw["score"]
    return hw


def _mean_sd(vals):
    return (statistics.mean(vals),
            statistics.stdev(vals) if len(vals) > 1 else 0.0)


def _tukey_fence(vals, k: float = 1.5):
    """Return (lower, upper, outliers, cleaned) using the classic
    Tukey 1.5xIQR rule. Uses statistics.quantiles (method='exclusive',
    close to the textbook definition) so results are reproducible without
    numpy."""
    if len(vals) < 4:
        return None, None, [], list(vals)
    q1, _, q3 = statistics.quantiles(vals, n=4, method="exclusive")
    iqr = q3 - q1
    lo, hi = q1 - k * iqr, q3 + k * iqr
    outliers = [(i, v) for i, v in enumerate(vals, start=1) if v < lo or v > hi]
    cleaned = [v for v in vals if lo <= v <= hi]
    return lo, hi, outliers, cleaned


def _robust_summary(vals, label: str) -> str:
    """Median/IQR + Tukey-fence outlier disclosure + trimmed mean/SD.

    Median/IQR is reported as the primary robust statistic because a
    handful of large spikes (see BUG 19) can otherwise dominate a plain
    mean/SD, especially at n>=100.
    """
    med = statistics.median(vals)
    lo, hi, outliers, cleaned = _tukey_fence(vals)
    m_all, sd_all = _mean_sd(vals)
    lines = [
        f"{label}",
        f"  n               = {len(vals)}",
        f"  median          = {med:.3f} s",
    ]
    if lo is not None:
        lines.append(f"  Tukey fence     = [{lo:.3f}, {hi:.3f}] s (1.5x IQR)")
        lines.append(f"  outliers        = {len(outliers)}/{len(vals)} "
                     f"({100*len(outliers)/len(vals):.0f}%)"
                     + (f"  runs: {[i for i, _ in outliers]}" if outliers else ""))
    lines.append(f"  mean/SD (all)   = {m_all:.3f} +/- {sd_all:.3f} s")
    if cleaned and len(cleaned) != len(vals):
        m_c, sd_c = _mean_sd(cleaned)
        lines.append(f"  mean/SD (trimmed, n={len(cleaned)}) = "
                     f"{m_c:.3f} +/- {sd_c:.3f} s")
    return "\n".join(lines)


def main(runs: int):
    mon = ResourceMonitor()
    mon.start()

    proc = psutil.Process()
    io_before = proc.io_counters() if hasattr(proc, "io_counters") else None

    rows, log = [], []
    for i in range(runs):
        hw = single_run()
        rows.append({k: v for k, v in hw["_timings"].items() if k != "score"})
        # BUFFERED: no I/O inside the measured region.
        log.append(f"  run {i+1:>3}/{runs}   "
                   f"total={hw['_timings']['total']:.3f}s   "
                   f"score={hw['score']}")

    io_after = proc.io_counters() if io_before else None
    mon.stop()

    disk_written = (io_after.write_bytes - io_before.write_bytes) \
        if io_before else "n/a"

    print("\n".join(log))

    # ---- raw CSV ------------------------------------------------------
    with open("benchmark_raw.csv", "w", newline="") as f:
        wtr = csv.DictWriter(f, fieldnames=STAGES + ["total"],
                             extrasaction="ignore")
        wtr.writeheader()
        wtr.writerows(rows)

    # ---- Table II -----------------------------------------------------
    w = config.BENCH_WARMUP_RUNS
    totals = [r["total"] for r in rows]
    steady = totals[w:] if len(totals) > w else totals
    mean_total, sd_total = _mean_sd(totals)
    mean_steady, sd_steady = _mean_sd(steady)

    lines = ["Stage".ljust(38) + "Mean(s)  SD(s)   Share(%)", "-" * 62]
    for s in STAGES:
        vals = [r[s] for r in rows][w:]
        m, sd = _mean_sd(vals)
        lines.append(f"{LABELS[s]:<38}{m:7.3f} {sd:7.3f} "
                     f"{100*m/mean_steady:8.1f}")
    lines.append("-" * 62)
    lines.append(f"{'TOTAL (steady state)':<38}{mean_steady:7.3f} "
                 f"{sd_steady:7.3f} {100.0:8.1f}")
    lines.append(f"{'TOTAL (incl. cold start)':<38}{mean_total:7.3f} "
                 f"{sd_total:7.3f}")
    table = "\n".join(lines)
    open("table_ii_latency.txt", "w").write(table)
    print("\n" + table)

    # ---- Robust statistics (median/IQR + Tukey-fence outliers) --------
    # See BUG 19: at larger n, sporadic high-latency runs (not explained
    # by cold-start alone) can dominate a plain mean/SD. This block gives
    # the paper an honest, reproducible way to report them instead of
    # silently dropping outliers or leaving an inflated SD unexplained.
    robust = _robust_summary(steady, "TOTAL execution time (steady state)")
    open("robust_stats.txt", "w").write(robust)
    print("\n" + robust)

    # ---- placeholders -------------------------------------------------
    n_cores = psutil.cpu_count(logical=True) or 1
    cpu_peak = max(s[1] for s in mon.samples) if mon.samples else 0.0
    cpu_sys = cpu_peak / n_cores
    rss_min = min(s[2] for s in mon.samples) if mon.samples else 0.0
    rss_max = max(s[2] for s in mon.samples) if mon.samples else 0.0

    med = statistics.median(steady)
    lo, hi, outliers, cleaned = _tukey_fence(steady)

    if lo is not None:
        fence_line = (f"     Tukey fence     = [{lo:.3f}, {hi:.3f}] s "
                     f"(1.5x IQR), {len(outliers)}/{len(steady)} runs beyond it")
    else:
        fence_line = "     Tukey fence     = n/a (fewer than 4 runs)"

    if cleaned and len(cleaned) != len(steady):
        mean_trimmed, sd_trimmed = _mean_sd(cleaned)
        trimmed_line = (f"<X'> trimmed mean   = {mean_trimmed:.3f} s   "
                        f"(n={len(cleaned)}, {len(outliers)} outliers excluded)\n"
                        f"<Y'> trimmed SD     = {sd_trimmed:.3f} s")
    else:
        trimmed_line = "<X'> trimmed mean   = n/a (no outliers beyond Tukey fence)"

    ph = f"""
Paste these into eco_itad.tex
=============================
<N>  runs            = {runs}  (warm-up discarded: {w})
<M>  nodes           = FILL IN MANUALLY (how many machines you tested)
<X>  mean total      = {mean_steady:.3f} s   (steady state)
<Y>  SD total        = {sd_steady:.3f} s   (steady state)
     incl. cold start= {mean_total:.3f} +/- {sd_total:.3f} s
<MED> median         = {med:.3f} s
{fence_line}
{trimmed_line}
     NOTE: if runs>=100, prefer median/IQR or the trimmed mean over the
           plain mean/SD above -- see robust_stats.txt and BUG 19 in this
           file's docstring for why.
<Z>  peak CPU        = {cpu_peak:.1f} % of ONE logical core
                       = {cpu_sys:.1f} % of the {n_cores}-core system
     NOTE: values slightly above 100% are a psutil sampling artefact,
           NOT Turbo Boost. Do not attribute them to frequency scaling.
<A>  min RSS         = {rss_min:.1f} MB
<B>  max RSS         = {rss_max:.1f} MB
     bytes written   = {disk_written}
     NOTE: cite verify_zero_write.py for the zero-write claim; this figure
           still includes interpreter and harness overhead.
"""
    open("paper_placeholders.txt", "w").write(ph)
    print(ph)

    # ---- Figure 1 -----------------------------------------------------
    fig, ax = plt.subplots(figsize=(3.4, 2.4), dpi=300)
    ax.hist(steady, bins=min(15, max(5, len(steady) // 4)),
            edgecolor="black", linewidth=0.5)
    ax.axvline(mean_steady, linestyle="--", linewidth=1.2,
               label=f"mean = {mean_steady:.3f} s")
    ax.set_xlabel("Execution time (s)", fontsize=8)
    ax.set_ylabel("Frequency", fontsize=8)
    ax.tick_params(labelsize=7)
    ax.legend(fontsize=7)
    fig.tight_layout()
    fig.savefig("fig_execution_time.pdf")

    # ---- Figure 2 -----------------------------------------------------
    if mon.samples:
        t = [s[0] for s in mon.samples]
        cpu = [s[1] for s in mon.samples]
        rss = [s[2] for s in mon.samples]
        fig, ax1 = plt.subplots(figsize=(3.4, 2.4), dpi=300)
        ax1.plot(t, cpu, linewidth=0.8)
        ax1.set_xlabel("Elapsed time (s)", fontsize=8)
        ax1.set_ylabel("CPU utilisation (% of one core)", fontsize=8)
        ax1.tick_params(labelsize=7)
        ax1.grid(alpha=0.3, linewidth=0.4)
        ax2 = ax1.twinx()
        ax2.plot(t, rss, linewidth=0.8, linestyle="--", color="tab:red")
        ax2.set_ylabel("Resident memory (MB)", fontsize=8)
        ax2.tick_params(labelsize=7)
        fig.tight_layout()
        fig.savefig("fig_resource_usage.pdf")

    print("\nDone. Figures: fig_execution_time.pdf, fig_resource_usage.pdf")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--runs", type=int, default=50)
    main(p.parse_args().runs)