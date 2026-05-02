#!/usr/bin/env python3
"""
Daily price refresh for the themes tracker dashboard.

Reads the current index.html, pulls fresh stock prices via yfinance,
recomputes per-period returns + per-bubble performance aggregates,
updates the 'Prices as of' date stamp, and writes the file back.

Run from the repo root. Designed to be invoked by GitHub Actions.
Idempotent: safe to re-run; if no tickers have new data, file is unchanged.
"""
import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf


HTML_PATH = Path(__file__).parent / "index.html"
DATA_SCRIPT_RE = re.compile(
    r'(<script id="data" type="application/json">)(.*?)(</script>)',
    re.DOTALL
)
DATE_STAMP_RE = re.compile(
    r'(<span id="prices-as-of" data-iso=")[^"]*(">)[^<]*(</span>)'
)


def fetch_returns(tickers: list[str]) -> dict:
    """Pull 3y of history per ticker; compute YTD/3M/6M/1Y/2Y returns."""
    out = {}
    failed = []

    for tkr in tickers:
        try:
            t = yf.Ticker(tkr)
            hist = t.history(period="3y", auto_adjust=True)

            if len(hist) < 5:
                failed.append((tkr, "insufficient history"))
                continue

            last_close = hist["Close"].iloc[-1]
            last_date = hist.index[-1]

            # YTD: from first trading day of the current calendar year
            ytd_data = hist[hist.index.year == last_date.year]
            if len(ytd_data) < 1:
                continue
            ytd_start = ytd_data.iloc[0]["Close"]
            ytd = (last_close / ytd_start - 1) * 100

            def back(days: int):
                target = last_date - pd.Timedelta(days=days)
                prior = hist[hist.index <= target]
                if len(prior) == 0:
                    return None
                return (last_close / prior["Close"].iloc[-1] - 1) * 100

            row = {
                "last": round(float(last_close), 2),
                "ret_ytd": round(float(ytd), 2),
                "ret_3m": _safe_round(back(91)),
                "ret_6m": _safe_round(back(182)),
                "ret_1y": _safe_round(back(365)),
                "ret_2y": _safe_round(back(730)),
            }
            out[tkr] = row
        except Exception as e:
            failed.append((tkr, str(e)[:80]))
            continue

    if failed:
        print(f"  WARNING: {len(failed)} tickers failed:")
        for tkr, err in failed[:10]:
            print(f"    {tkr}: {err}")
        if len(failed) > 10:
            print(f"    ... and {len(failed) - 10} more")

    return out


def _safe_round(v):
    return None if v is None else round(float(v), 2)


def recompute_bubble_perf(bubble: dict, ticker_returns: dict, spy: dict) -> None:
    """Recompute per-bubble avg_ytd, ytd_vs_spy, perf_by_period in place."""
    tickers = bubble.get("top_tickers_norm") or bubble.get("top_tickers") or []

    perf = {}
    for period in ["ytd", "3m", "6m", "1y", "2y"]:
        key = f"ret_{period}"
        valid = [
            ticker_returns[t][key]
            for t in tickers
            if t in ticker_returns and ticker_returns[t].get(key) is not None
        ]
        if valid:
            avg = sum(valid) / len(valid)
            spy_ret = spy.get(key)
            perf[period] = {
                "avg": round(avg, 2),
                "vs_spy": round(avg - spy_ret, 2) if spy_ret is not None else None,
                "n": len(valid),
            }
        else:
            perf[period] = None

    bubble["perf_by_period"] = perf

    # Backward-compat fields used by tooltip + detail panel
    if perf.get("ytd"):
        bubble["avg_ytd"] = perf["ytd"]["avg"]
        bubble["ytd_vs_spy"] = perf["ytd"]["vs_spy"]


def main() -> int:
    if not HTML_PATH.exists():
        print(f"ERROR: {HTML_PATH} not found", file=sys.stderr)
        return 1

    print(f"Reading {HTML_PATH} ({HTML_PATH.stat().st_size // 1024} KB)")
    html = HTML_PATH.read_text(encoding="utf-8")

    m = DATA_SCRIPT_RE.search(html)
    if not m:
        print("ERROR: could not find <script id=\"data\"> block", file=sys.stderr)
        return 2

    print("Parsing embedded data payload")
    payload = json.loads(m.group(2))

    # Collect all tickers used by any bubble + SPY benchmark
    tickers = set(["SPY"])
    for b in payload.get("bubbles", []):
        for t in (b.get("top_tickers_norm") or b.get("top_tickers") or []):
            if t:
                tickers.add(t)

    tickers = sorted(tickers)
    print(f"Refreshing prices for {len(tickers)} tickers")
    new_returns = fetch_returns(tickers)

    if "SPY" not in new_returns:
        print("ERROR: SPY fetch failed; aborting", file=sys.stderr)
        return 3

    print(f"Got fresh data for {len(new_returns)}/{len(tickers)} tickers")

    # Preserve any tickers we couldn't fetch this run by keeping their old rows
    old_returns = payload.get("ticker_returns", {})
    merged = dict(old_returns)
    merged.update(new_returns)
    payload["ticker_returns"] = merged

    # Recompute bubble-level performance aggregates
    spy = merged["SPY"]
    for b in payload["bubbles"]:
        recompute_bubble_perf(b, merged, spy)

    # Re-embed the payload
    new_payload_str = json.dumps(payload, separators=(",", ":"))
    new_html = (
        html[:m.start(2)]
        + new_payload_str
        + html[m.end(2):]
    )

    # Update the date stamp using NY time (markets close ~4pm EST)
    ny_now = datetime.now(timezone.utc) - timedelta(hours=5)  # EST approx
    today_iso = ny_now.strftime("%Y-%m-%d")
    new_html = DATE_STAMP_RE.sub(
        rf'\g<1>{today_iso}\g<2>{today_iso}\g<3>',
        new_html,
    )

    if new_html == html:
        print("No changes; file is identical. Nothing to commit.")
        return 0

    HTML_PATH.write_text(new_html, encoding="utf-8")
    print(f"Wrote updated index.html ({HTML_PATH.stat().st_size // 1024} KB)")
    print(f"Date stamp set to {today_iso}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
