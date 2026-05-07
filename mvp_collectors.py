#!/usr/bin/env python3
"""
MVP collectors for «Заголовки против кассы».

Outputs:
- polymarket_raw.csv
- rbc_raw.csv
- run_meta.json
- raw JSON/XML snapshots

Dependencies: Python 3.10+, standard library only.

Usage:
  python mvp_collectors.py --out data --pm-limit 100 --pm-pages 2
  python mvp_collectors.py --out data --pm-limit 100 --pm-pages 2 --with-history
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
RBC_RSS_URL = "https://rssexport.rbc.ru/rbcnews/news/30/full.rss"


def now_utc_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def now_msk_iso() -> str:
    msk = dt.timezone(dt.timedelta(hours=3))
    return dt.datetime.now(msk).replace(microsecond=0).isoformat()


def http_get_json(url: str, timeout: int = 30) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "media-vs-market-mvp/0.1 (+manual research; no trading)",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
        return json.loads(body.decode("utf-8"))


def http_get_bytes(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "media-vs-market-mvp/0.1 (+manual research)",
            "Accept": "*/*",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def parse_jsonish(value: Any) -> List[Any]:
    """Polymarket sometimes returns arrays as JSON strings. Humans, naturally, enjoy variety."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def as_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if value in ("", None):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def pick(d: Dict[str, Any], keys: Iterable[str], default: Any = None) -> Any:
    for key in keys:
        if key in d and d[key] not in (None, ""):
            return d[key]
    return default


def build_url(base: str, path: str, params: Dict[str, Any]) -> str:
    clean = {k: v for k, v in params.items() if v is not None}
    return f"{base}{path}?{urllib.parse.urlencode(clean)}"


def fetch_polymarket_events(limit: int, pages: int, order: str) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for page in range(pages):
        offset = page * limit
        url = build_url(
            GAMMA_BASE,
            "/events",
            {
                "active": "true",
                "closed": "false",
                "order": order,
                "ascending": "false",
                "limit": limit,
                "offset": offset,
            },
        )
        print(f"[pm] GET {url}", file=sys.stderr)
        chunk = http_get_json(url)
        if not isinstance(chunk, list):
            print(f"[pm] unexpected response type: {type(chunk)}", file=sys.stderr)
            continue
        events.extend(chunk)
        if len(chunk) < limit:
            break
        time.sleep(0.25)
    return events


def extract_tags(event: Dict[str, Any], market: Dict[str, Any]) -> str:
    tags = []
    for obj in [event, market]:
        for raw in [obj.get("tags"), obj.get("categories")]:
            if isinstance(raw, list):
                for item in raw:
                    if isinstance(item, dict):
                        name = item.get("label") or item.get("name") or item.get("slug")
                        if name:
                            tags.append(str(name))
                    elif isinstance(item, str):
                        tags.append(item)
    # Deduplicate, preserve order
    seen = set()
    out = []
    for tag in tags:
        if tag not in seen:
            out.append(tag)
            seen.add(tag)
    return "; ".join(out)


def normalize_polymarket(events: List[Dict[str, Any]], out_raw_dir: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    collected_at = now_utc_iso()

    for event in events:
        event_id = str(pick(event, ["id", "eventId"], ""))
        event_slug = str(event.get("slug") or "")
        event_title = str(event.get("title") or event.get("question") or "")
        event_url = f"https://polymarket.com/event/{event_slug}" if event_slug else ""

        raw_path = ""
        if event_id or event_slug:
            safe_name = (event_slug or event_id).replace("/", "_")[:160]
            raw_path = str(out_raw_dir / f"polymarket_event_{safe_name}.json")
            Path(raw_path).write_text(json.dumps(event, ensure_ascii=False, indent=2), encoding="utf-8")

        markets = event.get("markets") or []
        if not isinstance(markets, list):
            markets = []

        # Some API responses may represent an event as a direct market-like object.
        if not markets and any(k in event for k in ["outcomes", "outcomePrices", "question"]):
            markets = [event]

        for market in markets:
            outcomes = parse_jsonish(market.get("outcomes"))
            outcome_prices = parse_jsonish(market.get("outcomePrices"))
            clob_token_ids = parse_jsonish(market.get("clobTokenIds"))

            yes_idx = 0
            no_idx = 1 if len(outcome_prices) > 1 else None
            yes_price = as_float(outcome_prices[yes_idx] if len(outcome_prices) > yes_idx else None)
            no_price = as_float(outcome_prices[no_idx] if no_idx is not None and len(outcome_prices) > no_idx else None)

            yes_probability = round(yes_price * 100, 2) if yes_price is not None else None
            no_probability = round(no_price * 100, 2) if no_price is not None else (round(100 - yes_probability, 2) if yes_probability is not None else None)

            market_id = str(pick(market, ["id", "marketId", "conditionId"], ""))
            market_slug = str(market.get("slug") or event_slug or "")
            market_url = f"https://polymarket.com/event/{event_slug}" if event_slug else ""

            last_price = as_float(pick(market, ["lastTradePrice", "last_price", "lastPrice", "price"], None))
            best_bid = as_float(pick(market, ["bestBid", "best_bid"], None))
            best_ask = as_float(pick(market, ["bestAsk", "best_ask"], None))
            spread_pp = None
            if best_bid is not None and best_ask is not None:
                spread_pp = round((best_ask - best_bid) * 100, 2)

            rows.append({
                "collected_at_utc": collected_at,
                "event_id": event_id,
                "event_slug": event_slug,
                "event_title": event_title,
                "market_id": market_id,
                "market_slug": market_slug,
                "market_question": str(market.get("question") or event_title),
                "category_tags": extract_tags(event, market),
                "event_url": event_url,
                "market_url": market_url,
                "outcomes_json": json.dumps(outcomes, ensure_ascii=False),
                "outcome_prices_json": json.dumps(outcome_prices, ensure_ascii=False),
                "yes_probability_0_100": yes_probability,
                "no_probability_0_100": no_probability,
                "last_price": last_price,
                "best_bid": best_bid,
                "best_ask": best_ask,
                "spread_pp": spread_pp,
                "volume_24h_usd": as_float(pick(market, ["volume24hr", "volume_24hr", "volume24h", "volume24hrClob"], None)),
                "volume_7d_usd": as_float(pick(market, ["volume1wk", "volume_7d", "volume7d"], None)),
                "volume_total_usd": as_float(pick(market, ["volume", "volumeNum", "volumeClob"], None)),
                "liquidity_usd": as_float(pick(market, ["liquidity", "liquidityNum", "liquidityClob"], None)),
                "open_interest_usd": as_float(pick(market, ["openInterest", "open_interest"], None)),
                "active": pick(market, ["active"], event.get("active")),
                "closed": pick(market, ["closed"], event.get("closed")),
                "enable_orderbook": pick(market, ["enableOrderBook"], None),
                "end_date_utc": pick(market, ["endDate", "end_date_iso", "end_date"], event.get("endDate")),
                "resolution_source": pick(market, ["resolutionSource", "resolution_source"], ""),
                "raw_event_json_path": raw_path,
                "notes": "",
                # useful for optional history, not part of default sheet but kept if wanted
                "yes_token_id": str(clob_token_ids[0]) if clob_token_ids else "",
            })
    return rows


def fetch_price_history(asset_id: str, days: int = 8) -> List[Dict[str, Any]]:
    if not asset_id:
        return []
    end_ts = int(time.time())
    start_ts = end_ts - days * 86400
    url = build_url(
        CLOB_BASE,
        "/prices-history",
        {
            "market": asset_id,
            "startTs": start_ts,
            "endTs": end_ts,
            "interval": "1d",
            "fidelity": 1440,
        },
    )
    try:
        data = http_get_json(url)
        hist = data.get("history", []) if isinstance(data, dict) else []
        return hist if isinstance(hist, list) else []
    except Exception as exc:
        print(f"[pm-history] failed for {asset_id}: {exc}", file=sys.stderr)
        return []


def add_history_deltas(rows: List[Dict[str, Any]], max_rows: int = 100) -> None:
    for i, row in enumerate(rows[:max_rows]):
        asset_id = row.get("yes_token_id", "")
        hist = fetch_price_history(asset_id, days=8)
        if hist:
            current = row.get("yes_probability_0_100")
            prices = [(as_float(p.get("p")), p.get("t")) for p in hist if as_float(p.get("p")) is not None]
            prices = [(p, t) for p, t in prices if p is not None]
            if current is not None and prices:
                # API returns prices in 0..1, convert to 0..100
                last_24 = prices[-2][0] * 100 if len(prices) >= 2 else None
                last_7d = prices[0][0] * 100 if len(prices) >= 1 else None
                row["yes_delta24_pp"] = round(current - last_24, 2) if last_24 is not None else ""
                row["yes_delta7_pp"] = round(current - last_7d, 2) if last_7d is not None else ""
        time.sleep(0.15)


def fetch_rbc_rss(out_raw_dir: Path) -> List[Dict[str, Any]]:
    print(f"[rbc] GET {RBC_RSS_URL}", file=sys.stderr)
    xml_bytes = http_get_bytes(RBC_RSS_URL)
    raw_path = out_raw_dir / "rbc_full_rss.xml"
    raw_path.write_bytes(xml_bytes)

    root = ET.fromstring(xml_bytes)
    rows: List[Dict[str, Any]] = []
    collected_at = now_msk_iso()

    # RSS 2.0 usually: channel/item
    for idx, item in enumerate(root.findall(".//item"), start=1):
        def text(tag: str) -> str:
            el = item.find(tag)
            return (el.text or "").strip() if el is not None else ""

        title = text("title")
        link = text("link")
        pub_date = text("pubDate")
        desc = text("description")
        category = text("category")
        item_id = text("guid") or link or f"rbc-{idx}"

        rows.append({
            "collected_at_msk": collected_at,
            "item_id": item_id,
            "source_type": "rss",
            "channel_or_feed": "rbc_full_rss",
            "published_at_msk": pub_date,
            "category": category,
            "headline": title,
            "summary": desc,
            "url": link,
            "author_or_source": "РБК",
            "tags": "",
            "entities_manual": "",
            "topic_cluster": "",
            "media_tone": "",
            "media_intensity_0_100": "",
            "claim_probability_0_100": "",
            "narrative_direction_yes_no": "",
            "quote_or_expert": "",
            "is_sponsored": "",
            "raw_text_path": str(raw_path),
            "notes": "",
        })
    return rows


def write_csv(path: Path, rows: List[Dict[str, Any]], fieldnames: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


PM_FIELDS = [
    "collected_at_utc","event_id","event_slug","event_title","market_id","market_slug",
    "market_question","category_tags","event_url","market_url","outcomes_json","outcome_prices_json",
    "yes_probability_0_100","no_probability_0_100","last_price","best_bid","best_ask","spread_pp",
    "volume_24h_usd","volume_7d_usd","volume_total_usd","liquidity_usd","open_interest_usd",
    "active","closed","enable_orderbook","end_date_utc","resolution_source","raw_event_json_path",
    "yes_delta24_pp","yes_delta7_pp","notes"
]

RBC_FIELDS = [
    "collected_at_msk","item_id","source_type","channel_or_feed","published_at_msk","category",
    "headline","summary","url","author_or_source","tags","entities_manual","topic_cluster",
    "media_tone","media_intensity_0_100","claim_probability_0_100","narrative_direction_yes_no",
    "quote_or_expert","is_sponsored","raw_text_path","notes"
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="data", help="Output directory")
    parser.add_argument("--pm-limit", type=int, default=100)
    parser.add_argument("--pm-pages", type=int, default=1)
    parser.add_argument("--pm-order", default="volume_24hr", choices=["volume_24hr", "volume", "liquidity", "start_date", "end_date", "competitive", "closed_time"])
    parser.add_argument("--with-history", action="store_true", help="Fetch CLOB price history for top rows to compute deltas")
    parser.add_argument("--history-max-rows", type=int, default=100)
    args = parser.parse_args()

    out_dir = Path(args.out)
    raw_dir = out_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    meta = {
        "started_at_utc": now_utc_iso(),
        "polymarket_events": 0,
        "polymarket_markets": 0,
        "rbc_items": 0,
        "errors": [],
    }

    try:
        events = fetch_polymarket_events(limit=args.pm_limit, pages=args.pm_pages, order=args.pm_order)
        meta["polymarket_events"] = len(events)
        (raw_dir / "polymarket_events_snapshot.json").write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")
        pm_rows = normalize_polymarket(events, raw_dir)
        if args.with_history:
            add_history_deltas(pm_rows, max_rows=args.history_max_rows)
        meta["polymarket_markets"] = len(pm_rows)
        write_csv(out_dir / "polymarket_raw.csv", pm_rows, PM_FIELDS)
    except Exception as exc:
        meta["errors"].append(f"polymarket: {exc}")
        print(f"[error] polymarket: {exc}", file=sys.stderr)

    try:
        rbc_rows = fetch_rbc_rss(raw_dir)
        meta["rbc_items"] = len(rbc_rows)
        write_csv(out_dir / "rbc_raw.csv", rbc_rows, RBC_FIELDS)
    except Exception as exc:
        meta["errors"].append(f"rbc: {exc}")
        print(f"[error] rbc: {exc}", file=sys.stderr)

    meta["finished_at_utc"] = now_utc_iso()
    (out_dir / "run_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(meta, ensure_ascii=False, indent=2))
    return 0 if not meta["errors"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
