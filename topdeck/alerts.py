"""Smart price alerts: explainable arithmetic on price history.

Two rules, both computed over the N snapshots before the current price,
both in integer cents, no models and no new dependencies:

- Moving-average deviation: the current headline price (market when
  present, else mid) vs the window mean. Fires when the deviation is
  more than the threshold percent.
- Bollinger-style bands: mean +/- k * stddev over the same window.
  Fires when the current price sits above the upper band or below the
  lower band.

Minimum-history rule: fewer than MIN_BASELINE_POINTS baseline points
means no smart alerts for that card. Saying nothing beats alerting on
noise, so thin history stays silent rather than guessing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from topdeck import backbone

#: Defaults for `topdeck check --smart`.
DEFAULT_WINDOW = 14
DEFAULT_DEVIATION_PCT = 15.0
DEFAULT_BAND_K = 2.0

#: Baseline snapshots needed before any smart alert fires. The current
#: price is compared against the window; it is never part of it.
MIN_BASELINE_POINTS = 5


@dataclass
class SmartAlert:
    """One fired rule, human-readable and machine-readable.

    Money is integer cents everywhere, so the arithmetic is exact.
    mean_cents and the band edges are floats only because a mean is
    rarely a whole cent.
    """

    rule: str  # "avg_deviation", "band_high", or "band_low"
    line: str  # the rule stated plainly, e.g. "14d avg $4.20, now $5.10 (+21.4%)"
    window: int
    baseline_points: int
    mean_cents: float
    current_cents: int
    deviation_pct: float | None
    band_k: float
    upper_band_cents: float
    lower_band_cents: float

    def to_dict(self) -> dict:
        return {
            "rule": self.rule,
            "line": self.line,
            "window": self.window,
            "baseline_points": self.baseline_points,
            "mean_cents": round(self.mean_cents, 2),
            "current_cents": self.current_cents,
            "deviation_pct": (None if self.deviation_pct is None else round(self.deviation_pct, 2)),
            "band_k": self.band_k,
            "upper_band_cents": round(self.upper_band_cents, 2),
            "lower_band_cents": round(self.lower_band_cents, 2),
        }


def _dollars(cents: float) -> str:
    return f"${cents / 100:,.2f}"


def _headline(
    history: list[tuple[str, int | None, int | None, str]],
) -> list[tuple[str, int]]:
    """One headline price per day: market cents preferred, mid fallback.

    Days with no price at all are skipped, never treated as zero.
    """
    points: list[tuple[str, int]] = []
    for date, market_cents, mid_cents, _source in history:
        if market_cents is not None:
            points.append((date, market_cents))
        elif mid_cents is not None:
            points.append((date, mid_cents))
    return points


def evaluate(
    history: list[tuple[str, int | None, int | None, str]],
    *,
    window: int = DEFAULT_WINDOW,
    deviation_pct: float = DEFAULT_DEVIATION_PCT,
    band_k: float = DEFAULT_BAND_K,
) -> list[SmartAlert]:
    """Fire the smart-alert rules on one card's price history.

    The baseline is the last `window` snapshots before the current one;
    the current price is always the newest snapshot and never part of
    the baseline. With a daily `topdeck check` from cron, the window is
    a genuine N-day average. Fewer than MIN_BASELINE_POINTS baseline
    snapshots means no alerts, however the window is set.
    """
    points = _headline(history)
    if len(points) < MIN_BASELINE_POINTS + 1:
        return []
    baseline = points[-(window + 1) : -1]
    if len(baseline) < MIN_BASELINE_POINTS:
        return []
    current = points[-1][1]
    count = len(baseline)
    mean = sum(cents for _, cents in baseline) / count

    deviation = (current - mean) / mean * 100 if mean > 0 else None
    variance = sum((cents - mean) ** 2 for _, cents in baseline) / count
    stddev = math.sqrt(variance)
    upper = mean + band_k * stddev
    lower = mean - band_k * stddev

    fired: list[SmartAlert] = []
    if deviation is not None and abs(deviation) > deviation_pct:
        fired.append(
            SmartAlert(
                rule="avg_deviation",
                line=(
                    f"{window}d avg {_dollars(mean)}, now {_dollars(current)} ({deviation:+.1f}%)"
                ),
                window=window,
                baseline_points=count,
                mean_cents=mean,
                current_cents=current,
                deviation_pct=deviation,
                band_k=band_k,
                upper_band_cents=upper,
                lower_band_cents=lower,
            )
        )
    if current > upper:
        fired.append(
            SmartAlert(
                rule="band_high",
                line=(
                    f"above {window}d band {_dollars(upper)}, "
                    f"mean {_dollars(mean)} +/- {band_k:g} std"
                ),
                window=window,
                baseline_points=count,
                mean_cents=mean,
                current_cents=current,
                deviation_pct=deviation,
                band_k=band_k,
                upper_band_cents=upper,
                lower_band_cents=lower,
            )
        )
    elif current < lower:
        fired.append(
            SmartAlert(
                rule="band_low",
                line=(
                    f"below {window}d band {_dollars(lower)}, "
                    f"mean {_dollars(mean)} +/- {band_k:g} std"
                ),
                window=window,
                baseline_points=count,
                mean_cents=mean,
                current_cents=current,
                deviation_pct=deviation,
                band_k=band_k,
                upper_band_cents=upper,
                lower_band_cents=lower,
            )
        )
    return fired


def check_card(
    game: str,
    join_key: int,
    *,
    window: int = DEFAULT_WINDOW,
    deviation_pct: float = DEFAULT_DEVIATION_PCT,
    band_k: float = DEFAULT_BAND_K,
) -> list[SmartAlert]:
    """Evaluate the smart rules for one game/join_key.

    The full history is read and the window is applied by snapshot
    count, not calendar days: sparse history (checks less often than
    daily) still alerts on real moves instead of starving the window.
    """
    history = backbone.get_history(game, join_key)
    return evaluate(history, window=window, deviation_pct=deviation_pct, band_k=band_k)
