"""Benchmark: time one lookup per adapter against the live price APIs.

Manual use only, not part of CI (pytest only collects tests/):

    python scripts/benchmark_lookups.py            # warm cache
    TOPDECK_BENCH_COLD=1 python scripts/benchmark_lookups.py   # cold cache

Reports per-adapter latency for search and for pricing the recommended
hit, plus how many HTTP requests actually went out versus cache hits.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import urllib.request

if os.environ.get("TOPDECK_BENCH_COLD"):
    os.environ["XDG_CACHE_HOME"] = tempfile.mkdtemp(prefix="topdeck-bench-")
    print("cold run: fresh cache dir", os.environ["XDG_CACHE_HOME"])

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import topdeck.net as net
from topdeck import adapters as game_adapters

QUERIES = {
    "mtg": "Lightning Bolt",
    "pokemon": "Pikachu",
    "lorcana": "Mickey",
    "onepiece": "Zoro",
    "riftbound": "Ahri",
}

_fetch_calls = 0
_http_calls = 0
_real_urlopen = urllib.request.urlopen


def counting_urlopen(req, *args, **kwargs):
    global _http_calls
    _http_calls += 1
    return _real_urlopen(req, *args, **kwargs)


urllib.request.urlopen = counting_urlopen
_real_fetch = net.fetch_json


def counting_fetch(url, **kwargs):
    global _fetch_calls
    _fetch_calls += 1
    return _real_fetch(url, **kwargs)


net.fetch_json = counting_fetch
# adapters call net.fetch_json via the module attribute, so this counts.


def main() -> int:
    print(
        f"{'game':<10} {'search s':>8} {'prices s':>8} {'hits':>5} {'prices':>7} "
        f"{'fetch':>6} {'http':>5}"
    )
    print("-" * 62)
    totals = [0.0, 0.0, 0, 0]
    for key in ["mtg", "pokemon", "lorcana", "onepiece", "riftbound"]:
        adapter = game_adapters.REGISTRY[key]
        f0, h0 = _fetch_calls, _http_calls
        t0 = time.perf_counter()
        try:
            hits = adapter.search(QUERIES[key])
        except net.SourceError as exc:
            print(f"{key:<10} FAILED: {exc}")
            continue
        t1 = time.perf_counter()
        ranked = game_adapters.rank_candidates(hits, QUERIES[key])
        n_prices = 0
        if ranked:
            try:
                prices = adapter.get_prices(ranked[0])
                n_prices = len(prices)
            except net.SourceError as exc:
                print(f"{key:<10} price fetch failed: {exc}")
        t2 = time.perf_counter()
        df, dh = _fetch_calls - f0, _http_calls - h0
        totals[0] += t1 - t0
        totals[1] += t2 - t1
        totals[2] += df
        totals[3] += dh
        name = ranked[0].name if ranked else "-"
        print(
            f"{key:<10} {t1 - t0:>8.1f} {t2 - t1:>8.1f} {len(ranked):>5} "
            f"{n_prices:>7} {df:>6} {dh:>5}  e.g. {name[:32]}"
        )
    print("-" * 62)
    print(
        f"{'total':<10} {totals[0]:>8.1f} {totals[1]:>8.1f} {'':>5} {'':>7} "
        f"{totals[2]:>6} {totals[3]:>5}"
    )
    print(
        f"fetch_json calls: {totals[2]}, real HTTP requests: {totals[3]} "
        f"({totals[2] - totals[3]} served from cache)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
