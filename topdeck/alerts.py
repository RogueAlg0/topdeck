"""Smart price alerts: explainable arithmetic on price history.

Two rules, both computed over the N snapshots before the current price,
both in integer cents, no models and no new dependencies:

- Moving-average deviation: the current headline price (market when
  present, else mid) vs the window mean. Fires when the deviation is
  more than the threshold percent.
- Bollinger-style bands: mean +/- k * stddev over the same window.
  Fires when the current price sits above the upper band or below the
  lower band.

Glitch guard: one bad print must not move the baseline or fake a
spike. Tukey 1.5*IQR fences (the same rule the price table uses for its
"!" markers) split the baseline into inliers and suspected glitches;
the mean and the bands are computed on inliers only. A current price
outside the fences is treated as a glitch until a second consecutive
snapshot confirms it: a real move persists at the new level, a bad
print reverts.

Minimum-history rule: fewer than MIN_BASELINE_POINTS trustworthy
baseline points means no smart alerts for that card. Saying nothing
beats alerting on noise, so thin history stays silent rather than
guessing.
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


def _percentile(ordered: list[float], pct: float) -> float:
    """Linear-interpolation percentile over an already sorted list."""
    rank = pct / 100 * (len(ordered) - 1)
    low = int(rank)
    frac = rank - low
    return ordered[low] * (1 - frac) + ordered[low + 1] * frac


def _tukey_fences(values: list[int]) -> tuple[float, float]:
    """The 1.5*IQR fences around the baseline snapshots.

    Anything outside the fences is a suspected glitch print: it is cut
    from the baseline before any statistic is computed, so one bad
    number can neither inflate the average nor fake a spike. The
    current price is judged against the same fences.
    """
    ordered = sorted(values)
    q1 = _percentile(ordered, 25)
    q3 = _percentile(ordered, 75)
    iqr = q3 - q1
    return q1 - 1.5 * iqr, q3 + 1.5 * iqr


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
    a genuine N-day average. Fewer than MIN_BASELINE_POINTS trustworthy
    baseline snapshots means no alerts, however the window is set.

    Glitch guard, in two parts. First, Tukey-outlier snapshots are cut
    from the baseline before the mean and the bands are computed, so a
    bad print in history cannot inflate either. Second, a current price
    outside the fences needs a second consecutive snapshot on the same
    side, nearer to the new price than to the old average: a real move
    persists at the new level while a glitch reverts, so a single wild
    print stays silent.
    """
    points = _headline(history)
    if len(points) < MIN_BASELINE_POINTS + 1:
        return []
    baseline = [cents for _, cents in points[-(window + 1) : -1]]
    if len(baseline) < MIN_BASELINE_POINTS:
        return []
    low, high = _tukey_fences(baseline)
    inliers = [cents for cents in baseline if low <= cents <= high]
    if len(inliers) < MIN_BASELINE_POINTS:
        return []
    current = points[-1][1]
    previous = points[-2][1]
    count = len(inliers)
    mean = sum(inliers) / count
    if current < low or current > high:
        same_side = (previous < low and current < low) or (previous > high and current > high)
        persists = abs(current - previous) < abs(current - mean)
        if not (same_side and persists):
            return []

    deviation = (current - mean) / mean * 100 if mean > 0 else None
    variance = sum((cents - mean) ** 2 for cents in inliers) / count
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
