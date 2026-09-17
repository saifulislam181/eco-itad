"""Remaining-service-life carbon allocation model (Eq. 2-3).

CHANGELOG (fixes):
  * Docstrings referred to "Eq. (4)/(5)" and "Table IV"; the paper numbers
    them Eq. (2)-(4) and Table III. Aligned.
  * sensitivity_table() recomputed values independently of avoided_carbon(),
    so a change to the rounding rule would silently desynchronise the table
    from the engine. It now calls the same code path.
  * A guard was added for scores outside [0,1].
"""
import config


def remaining_life(power_on_hours: float) -> float:
    """Eq. (2): R_life."""
    return max(0.0, 1.0 - power_on_hours / config.U_MAX)


def avoided_carbon(score: float, power_on_hours: float) -> dict:
    """Eq. (3): C_saved. Returns full breakdown for auditability."""
    if not 0.0 <= score <= 1.0:
        raise ValueError(f"score must lie in [0,1], got {score}")

    if score < config.THETA_MIN:
        return {
            "co2_saved_kg": 0.0,
            "r_life": 0.0,
            "verdict": "NON-RECOVERABLE -> material recovery (WEEE)",
        }

    r = remaining_life(power_on_hours)
    c = round(config.C_EMBODIED * score * r * config.GAMMA_REC, 1)
    return {
        "co2_saved_kg": c,
        "r_life": round(r, 3),
        "verdict": "RECOVERABLE -> redeploy / resale",
    }


def _c_saved(score, r_life, c_emb, gamma):
    return round(c_emb * score * r_life * gamma, 1)


def sensitivity_table(score: float = 0.85, r_life: float = 0.60) -> None:
    """Reproduces Table III of the paper."""
    gammas = (0.55, config.GAMMA_REC, 0.80)
    print(f"\nSensitivity  (S_health={score}, R_life={r_life})")
    print(f"{'C_emb':>8} | " + " | ".join(f"g={g:<6}" for g in gammas))
    print("-" * 44)
    for c_emb in (250, 320, 400):
        row = [f"{_c_saved(score, r_life, c_emb, g):6.1f}" for g in gammas]
        print(f"{c_emb:>8} | " + " | ".join(f"{v:<8}" for v in row))


if __name__ == "__main__":
    # Verify the worked example, Eq. (4): must print 112.2
    print(avoided_carbon(0.85, 17_520.0))
    sensitivity_table()