"""
gex_monitor.py — Real-time GEX level monitor.

For WATCH/TRADE positions, checks every 5 min:
  CALLS: stock in positive GEX + declining toward support wall → ENTRY ZONE
  PUTS:  stock in positive GEX + rising toward resistance wall → ENTRY ZONE

GEX is cached per ticker (1h TTL) to avoid rate limits.
Price direction detected from last 3 one-minute candles.
Fires priority alert when conditions are met.
"""
import os, time, json
from datetime import datetime
from zoneinfo import ZoneInfo

# GEX cache: {ticker: {data: dict, fetched_at: float}}
_gex_cache: dict = {}
GEX_CACHE_TTL = 3600  # 1 hour

# Alert cooldown: {ticker: last_alerted_ts}
_alerted: dict = {}
COOLDOWN = 3600  # 1 alert per ticker per hour max


def _get_gex(ticker: str) -> dict | None:
    """Return GEX data, using cache if fresh."""
    now = time.time()
    cached = _gex_cache.get(ticker)
    if cached and (now - cached["fetched_at"]) < GEX_CACHE_TTL:
        return cached["data"]
    try:
        from fetcher import fetch_gex
        time.sleep(5)  # rate limit
        data = fetch_gex(ticker)
        if data:
            _gex_cache[ticker] = {"data": data, "fetched_at": now}
        return data
    except Exception as e:
        print(f"[GEX_MON] GEX fetch error {ticker}: {e}")
        return None


def _get_price_and_direction(ticker: str) -> tuple[float, str]:
    """
    Returns (current_price, direction).
    direction = "declining" | "rising" | "flat"
    Uses last 5 one-minute candles.
    """
    try:
        from fetcher import fetch_1min_candles
        candles = fetch_1min_candles(ticker, count=6)
        if len(candles) >= 4:
            px  = float(candles[-1]["close"])
            old = float(candles[-4]["close"])
            chg = (px - old) / old * 100
            if chg < -0.15:
                return px, "declining"
            elif chg > 0.15:
                return px, "rising"
            else:
                return px, "flat"
        elif candles:
            return float(candles[-1]["close"]), "flat"
    except: pass
    try:
        from fetcher import fetch_price
        px = fetch_price(ticker)
        return float(px or 0), "flat"
    except:
        return 0.0, "flat"


def _find_nearest_support(strikes: list, spot: float, min_gex: float = 2_000_000) -> dict | None:
    """Nearest positive GEX support BELOW spot."""
    candidates = sorted(
        [s for s in strikes
         if float(s["strike"]) < spot
         and float(s.get("net_gex", 0)) > min_gex],
        key=lambda s: float(s["strike"]),
        reverse=True
    )
    return candidates[0] if candidates else None


def _find_nearest_resistance(strikes: list, spot: float, min_gex: float = 2_000_000) -> dict | None:
    """Nearest positive GEX resistance ABOVE spot."""
    candidates = sorted(
        [s for s in strikes
         if float(s["strike"]) > spot
         and float(s.get("net_gex", 0)) > min_gex],
        key=lambda s: float(s["strike"])
    )
    return candidates[0] if candidates else None


def _format_alert(ticker: str, watch_entry: dict, spot: float,
                  direction: str, gex_level: dict, gex_data: dict,
                  regime: str) -> str:
    """Format the GEX entry zone alert."""
    strike_level = float(gex_level["strike"])
    level_gex    = float(gex_level.get("net_gex", 0))
    is_call      = "put" not in (watch_entry.get("option_type","call") or "call").lower()
    dist_pct     = abs(spot - strike_level) / spot * 100
    opt_strike   = watch_entry.get("strike","?")
    expiry       = watch_entry.get("expiry","?")
    score        = watch_entry.get("flow_score","?")
    verdict      = watch_entry.get("verdict","WATCH")
    flip         = gex_data.get("gamma_flip")

    gex_size = ("MASSIVE" if abs(level_gex) > 50_000_000
                else "Large" if abs(level_gex) > 10_000_000
                else "Moderate")

    now_et   = datetime.now(ZoneInfo("America/New_York"))
    flow_ts  = float(watch_entry.get("added_at", time.time()))
    days_ago = int((time.time() - flow_ts) / 86400)
    age_str  = f"Day {days_ago+1} since flow" if days_ago > 0 else "Same day as flow"

    if is_call:
        action = "declining toward dealer support"
        zone   = f"Support ${strike_level:.0f} (+{level_gex/1_000_000:.0f}M {gex_size})"
        reason = f"Dealers mechanically BUY at ${strike_level:.0f} → expect bounce"
        stop   = f"${strike_level * 0.99:.2f} (below support)"
    else:
        action = "rising toward dealer resistance"
        zone   = f"Resistance ${strike_level:.0f} (+{level_gex/1_000_000:.0f}M {gex_size})"
        reason = f"Dealers mechanically SELL at ${strike_level:.0f} → expect reversal"
        stop   = f"${strike_level * 1.01:.2f} (above resistance)"

    flip_str = f"Gamma flip: ${flip:.0f}" if flip else ""

    lines = [
        f"🎯 GEX ENTRY ZONE: {ticker}",
        f"Stock {action} ({dist_pct:.1f}% away)",
        f"📅 {age_str}",
        f"",
        f"📐 {regime.upper()} GEX — {zone}",
        f"   {reason}",
        f"   {flip_str}",
        f"",
        f"💵 Current: ${spot:.2f} | Entry: ~${spot:.2f}",
        f"🛑 Stop: {stop}",
        f"",
        f"👀 {ticker} {opt_strike}{'C' if is_call else 'P'} {expiry} [{score}/7 {verdict}]",
    ]
    return "\n".join(l for l in lines if l is not None)


def run_gex_monitor(watchlist: dict, send_fn=None):
    """
    Main monitor function. Call every 5 min during market hours.
    watchlist: dict of {ticker: watch_entry}
    send_fn: callable(msg, bot_token, chat_id)
    """
    bot_token  = os.environ.get("TELEGRAM_BOT_TOKEN","")
    trade_chat = os.environ.get("TELEGRAM_TRADE_CHAT_ID","")
    if not bot_token or not trade_chat:
        return

    now_et = datetime.now(ZoneInfo("America/New_York"))
    # Only run during market hours
    if now_et.weekday() >= 5:
        return
    if not (9 <= now_et.hour < 16):
        return

    alerts_sent = 0
    for ticker, entry in watchlist.items():
        try:
            # Skip if recently alerted
            last_alert = _alerted.get(ticker, 0)
            if time.time() - last_alert < COOLDOWN:
                continue

            # Skip stale (deeply ITM) positions
            strike_f = float(entry.get("strike", 0) or 0)
            is_call  = "put" not in (entry.get("option_type","call") or "call").lower()

            # Get price + direction
            spot, direction = _get_price_and_direction(ticker)
            if not spot:
                continue

            # Quick staleness check
            if strike_f > 0:
                itm_pct = (spot - strike_f)/strike_f*100 if is_call else (strike_f - spot)/strike_f*100
                if itm_pct > 5.0:
                    continue

            # Get GEX (cached)
            gex = _get_gex(ticker)
            if not gex:
                continue

            regime  = gex.get("regime","")
            strikes = gex.get("strikes",[])

            # Only fire in POSITIVE GEX regime (dealer fade = predictable support/resistance)
            if regime != "positive":
                continue

            fired = False
            if is_call and direction == "declining":
                # Look for pullback toward support
                supp = _find_nearest_support(strikes, spot)
                if supp:
                    dist = (spot - float(supp["strike"])) / spot * 100
                    if 0.3 <= dist <= 2.0:  # within 0.3-2% above support
                        msg = _format_alert(ticker, entry, spot, direction,
                                            supp, gex, regime)
                        if send_fn:
                            send_fn(msg, bot_token, trade_chat)
                        print(f"[GEX_MON] 🎯 {ticker} call entry zone — declining to support ${float(supp['strike']):.0f} ({dist:.1f}%)")
                        _alerted[ticker] = time.time()
                        fired = True
                        alerts_sent += 1

            elif not is_call and direction == "rising":
                # Look for rally toward resistance
                res = _find_nearest_resistance(strikes, spot)
                if res:
                    dist = (float(res["strike"]) - spot) / spot * 100
                    if 0.3 <= dist <= 2.0:  # within 0.3-2% below resistance
                        msg = _format_alert(ticker, entry, spot, direction,
                                            res, gex, regime)
                        if send_fn:
                            send_fn(msg, bot_token, trade_chat)
                        print(f"[GEX_MON] 🎯 {ticker} put entry zone — rising to resistance ${float(res['strike']):.0f} ({dist:.1f}%)")
                        _alerted[ticker] = time.time()
                        fired = True
                        alerts_sent += 1

        except Exception as e:
            print(f"[GEX_MON] Error {ticker}: {e}")

    if alerts_sent:
        print(f"[GEX_MON] Sent {alerts_sent} entry zone alerts")
