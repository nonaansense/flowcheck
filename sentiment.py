"""
Market sentiment analysis for FlowCheck.
Uses Finnhub + Tiingo + Yahoo Finance to avoid consuming Polygon rate limits.

/sentiment TICKER — returns full sentiment picture for a ticker.
"""
import os, requests, time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

def fh_key():
    return os.environ.get("FINNHUB_API_KEY","")

def tiingo_key():
    return os.environ.get("TIINGO_API_KEY","")

def fh_get(endpoint: str, params: dict) -> dict | None:
    key = fh_key()
    if not key:
        return None
    try:
        r = requests.get(
            f"https://finnhub.io/api/v1{endpoint}",
            params={**params, "token": key},
            timeout=8
        )
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f"[SENTIMENT] Finnhub error {endpoint}: {e}")
    return None

# ── Price + Volume ─────────────────────────────────────────────────────

def fetch_price_data(ticker: str) -> dict:
    """Get price data from Finnhub with correct as-of date."""
    from market_calendar import is_market_open, today_et
    from datetime import timedelta
    result = {}
    try:
        q = fh_get("/quote", {"symbol": ticker.upper()})
        if q:
            price      = q.get("c")
            prev_close = q.get("pc")
            high       = q.get("h")
            low        = q.get("l")
            timestamp  = q.get("t")  # Unix timestamp of last trade

            if price and prev_close and float(prev_close) > 0:
                pct = round(((float(price) - float(prev_close))
                             / float(prev_close)) * 100, 2)
                result["price"]      = price
                result["prev_close"] = prev_close
                result["pct_change"] = pct
                result["high"]       = high
                result["low"]        = low
                result["price_emoji"]= "🟢" if pct > 0 else "🔴"

                # Determine correct as-of label
                if is_market_open():
                    result["price_date_label"] = "today"
                    result["price_context"]    = ""
                else:
                    # Find the last trading day
                    if timestamp:
                        from datetime import datetime
                        from zoneinfo import ZoneInfo
                        last_trade_dt = datetime.fromtimestamp(
                            float(timestamp),
                            tz=ZoneInfo("America/New_York")
                        )
                        result["price_date_label"] = last_trade_dt.strftime("%b %d")
                        result["price_context"]    = " (last close)"
                    else:
                        # Walk back to find last market day
                        dt = today_et()
                        from market_calendar import is_market_open as _imo
                        for _ in range(7):
                            dt = dt - timedelta(days=1)
                            if dt.weekday() < 5:
                                from market_calendar import MARKET_HOLIDAYS
                                if dt not in MARKET_HOLIDAYS:
                                    break
                        from datetime import datetime as _dt
                        result["price_date_label"] = _dt(dt.year, dt.month, dt.day).strftime("%b %d")
                        result["price_context"]    = " (last close)"

    except Exception as e:
        print(f"[SENTIMENT] Price error: {e}")
    return result

def fetch_volume_data(ticker: str) -> dict:
    """Get volume vs average from Tiingo."""
    key = tiingo_key()
    if not key:
        return {}
    try:
        # Get last 30 days of daily data for avg volume
        to_date   = datetime.now().strftime("%Y-%m-%d")
        from_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        r = requests.get(
            f"https://api.tiingo.com/tiingo/daily/{ticker.upper()}/prices",
            params={"startDate": from_date, "endDate": to_date, "token": key},
            timeout=8
        )
        if r.status_code == 200:
            bars = r.json()
            if bars and len(bars) >= 5:
                vols    = [b.get("volume",0) for b in bars if b.get("volume")]
                avg_vol = sum(vols[:-1]) / len(vols[:-1]) if len(vols) > 1 else 0
                today_vol = vols[-1] if vols else 0
                if avg_vol > 0 and today_vol > 0:
                    vol_ratio = round(today_vol / avg_vol, 1)
                    result = {
                        "today_vol":  today_vol,
                        "avg_vol":    round(avg_vol),
                        "vol_ratio":  vol_ratio,
                    }
                    if vol_ratio >= 3:
                        result["vol_label"] = "Very unusual — 3x+ average 🚨"
                    elif vol_ratio >= 2:
                        result["vol_label"] = "Unusual — 2x average ⚠️"
                    elif vol_ratio >= 1.5:
                        result["vol_label"] = "Above average"
                    else:
                        result["vol_label"] = "Normal"
                    return result
    except Exception as e:
        print(f"[SENTIMENT] Volume error: {e}")
    return {}

# ── News Sentiment ─────────────────────────────────────────────────────

def fetch_news_sentiment(ticker: str) -> dict:
    """
    Fetch news and analyze sentiment using Finnhub sentiment endpoint.
    Falls back to headline keyword analysis if sentiment endpoint unavailable.
    """
    result = {"articles": [], "sentiment": None, "sentiment_emoji": ""}

    try:
        # Finnhub news sentiment (free tier)
        sent = fh_get("/news-sentiment", {"symbol": ticker.upper()})
        if sent and sent.get("buzz"):
            buzz       = sent.get("buzz", {})
            score      = sent.get("companyNewsScore", 0)
            articles   = buzz.get("articlesInLastWeek", 0)
            buzz_score = buzz.get("buzz", 0)
            sentiment  = sent.get("sentiment", {})
            bull_pct   = sentiment.get("bullishPercent", 0)
            bear_pct   = sentiment.get("bearishPercent", 0)

            if bull_pct > 0.6:
                sent_label = "Bullish"
                sent_emoji = "🟢"
            elif bear_pct > 0.6:
                sent_label = "Bearish"
                sent_emoji = "🔴"
            else:
                sent_label = "Mixed"
                sent_emoji = "⚪"

            result.update({
                "sentiment":       sent_label,
                "sentiment_emoji": sent_emoji,
                "bull_pct":        round(bull_pct * 100, 1),
                "bear_pct":        round(bear_pct * 100, 1),
                "articles_week":   articles,
                "buzz_score":      buzz_score,
                "news_score":      score,
            })

        # Recent headlines
        to_dt   = datetime.now()
        from_dt = to_dt - timedelta(days=3)
        news = fh_get("/company-news", {
            "symbol": ticker.upper(),
            "from":   from_dt.strftime("%Y-%m-%d"),
            "to":     to_dt.strftime("%Y-%m-%d"),
        })
        if news and isinstance(news, list):
            cutoff  = time.time() - 24 * 3600
            recent  = [
                a for a in news
                if a.get("datetime", 0) >= cutoff
                and ticker.lower() in (a.get("headline","") or "").lower()
            ]
            result["articles"]    = recent[:3]
            result["article_count"] = len(recent)

    except Exception as e:
        print(f"[SENTIMENT] News error: {e}")

    return result

# ── Insider Activity ───────────────────────────────────────────────────

def fetch_insider_sentiment(ticker: str) -> dict:
    """Get insider transactions and recommendation trends from Finnhub."""
    result = {}
    try:
        # Insider transactions
        txns = fh_get("/stock/insider-transactions",
                       {"symbol": ticker.upper()})
        if txns and txns.get("data"):
            cutoff = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
            recent = [
                t for t in txns["data"]
                if t.get("transactionDate","") >= cutoff
            ]
            buys  = [t for t in recent
                     if t.get("transactionType","") in ("Buy","P - Purchase")
                     and float(t.get("share",0) or 0) > 0]
            sells = [t for t in recent
                     if t.get("transactionType","") in ("Sell","S - Sale")
                     and float(t.get("share",0) or 0) > 0]

            if buys or sells:
                result["insider_buys"]  = len(buys)
                result["insider_sells"] = len(sells)
                if len(buys) > len(sells):
                    result["insider_label"] = "Net buying — bullish signal 🟢"
                elif len(sells) > len(buys):
                    result["insider_label"] = "Net selling — bearish signal 🔴"
                else:
                    result["insider_label"] = "Mixed activity"

        # Analyst recommendations
        recs = fh_get("/stock/recommendation",
                       {"symbol": ticker.upper()})
        if recs and isinstance(recs, list) and recs:
            latest = recs[0]
            strong_buy = latest.get("strongBuy", 0)
            buy        = latest.get("buy", 0)
            hold       = latest.get("hold", 0)
            sell       = latest.get("sell", 0)
            strong_sell= latest.get("strongSell", 0)
            total = strong_buy + buy + hold + sell + strong_sell
            if total > 0:
                bull_analysts = strong_buy + buy
                bear_analysts = sell + strong_sell
                result["analyst_buy"]    = bull_analysts
                result["analyst_hold"]   = hold
                result["analyst_sell"]   = bear_analysts
                result["analyst_total"]  = total
                result["analyst_period"] = latest.get("period","")

    except Exception as e:
        print(f"[SENTIMENT] Insider error: {e}")
    return result

# ── Technical Analysis ────────────────────────────────────────────────

def fetch_technical_analysis(ticker: str) -> dict:
    """
    Calculate key technical indicators from Tiingo daily data.
    Uses last 60 days to compute MAs, RSI, and trend.
    No Polygon calls — preserves rate limits for flow alerts.
    """
    key = tiingo_key()
    if not key:
        return {}
    try:
        to_date   = datetime.now().strftime("%Y-%m-%d")
        from_date = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
        r = requests.get(
            f"https://api.tiingo.com/tiingo/daily/{ticker.upper()}/prices",
            params={"startDate": from_date, "endDate": to_date, "token": key},
            timeout=10
        )
        if r.status_code != 200 or not r.json():
            return {}

        bars = r.json()
        # If empty or only 1 bar (weekend/holiday), extend lookback
        if len(bars) < 5:
            from_date2 = (datetime.now() - timedelta(days=120)).strftime("%Y-%m-%d")
            r2 = requests.get(
                f"https://api.tiingo.com/tiingo/daily/{ticker.upper()}/prices",
                params={"startDate": from_date2, "endDate": to_date, "token": key},
                timeout=10
            )
            if r2.status_code == 200 and r2.json():
                bars = r2.json()
            if len(bars) < 5:
                print(f"[SENTIMENT] Tiingo: insufficient data for {ticker} ({len(bars)} bars)")
                return {}
        closes = [float(b.get("adjClose") or b.get("close",0)) for b in bars]
        highs  = [float(b.get("adjHigh")  or b.get("high",0))  for b in bars]
        lows   = [float(b.get("adjLow")   or b.get("low",0))   for b in bars]
        vols   = [int(b.get("volume",0))  for b in bars]

        if len(closes) < 20:
            return {}

        current = closes[-1]
        result  = {"current_price": current}

        # ── Moving Averages ───────────────────────────────────────────
        def sma(data, n):
            if len(data) < n: return None
            return round(sum(data[-n:]) / n, 2)

        ma10  = sma(closes, 10)
        ma20  = sma(closes, 20)
        ma50  = sma(closes, min(50,  len(closes)))
        ma100 = sma(closes, min(100, len(closes)))
        ma200 = sma(closes, min(200, len(closes)))

        result["ma10"]  = ma10
        result["ma20"]  = ma20
        result["ma50"]  = ma50
        result["ma100"] = ma100
        result["ma200"] = ma200

        # Price vs MAs
        ma_signals = []
        for label, val, key in [
            ("10SMA",  ma10,  "ma10"),
            ("20SMA",  ma20,  "ma20"),
            ("50SMA",  ma50,  "ma50"),
            ("100SMA", ma100, "ma100"),
            ("200SMA", ma200, "ma200"),
        ]:
            if val:
                above = current > val
                pct   = round(((current - val) / val) * 100, 1)
                result["vs_" + key] = pct
                ma_signals.append((label, above, pct))

        above_count = sum(1 for _, a, _ in ma_signals if a)
        total_mas   = len(ma_signals)
        result["mas_above"] = above_count
        result["mas_total"] = total_mas

        if above_count == total_mas:
            result["ma_trend"] = "Above all MAs — strong uptrend 🟢"
        elif above_count >= total_mas * 0.75:
            result["ma_trend"] = "Above most MAs — uptrend 🟢"
        elif above_count >= total_mas * 0.5:
            result["ma_trend"] = "Mixed — consolidating ⚪"
        elif above_count > 0:
            result["ma_trend"] = "Below most MAs — downtrend 🔴"
        else:
            result["ma_trend"] = "Below all MAs — strong downtrend 🔴🔴"

        # ── RSI (14-period) ───────────────────────────────────────────
        if len(closes) >= 15:
            deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
            gains  = [d if d > 0 else 0 for d in deltas]
            losses = [-d if d < 0 else 0 for d in deltas]
            avg_gain = sum(gains[-14:]) / 14
            avg_loss = sum(losses[-14:]) / 14
            if avg_loss > 0:
                rs  = avg_gain / avg_loss
                rsi = round(100 - (100 / (1 + rs)), 1)
            else:
                rsi = 100.0
            result["rsi"] = rsi
            if rsi >= 70:
                result["rsi_label"] = "Overbought ⚠️"
            elif rsi >= 55:
                result["rsi_label"] = "Bullish"
            elif rsi >= 45:
                result["rsi_label"] = "Neutral"
            elif rsi >= 30:
                result["rsi_label"] = "Bearish"
            else:
                result["rsi_label"] = "Oversold — potential bounce 🟢"

        # ── VWAP (approximation using 20-day typical price × volume) ──
        # True intraday VWAP needs minute data. Use 20d as proxy.
        if len(closes) >= 20 and len(vols) >= 20:
            tp_vol = sum(
                ((highs[i] + lows[i] + closes[i]) / 3) * vols[i]
                for i in range(-20, 0)
            )
            tot_vol = sum(vols[-20:])
            if tot_vol > 0:
                vwap = round(tp_vol / tot_vol, 2)
                result["vwap_20d"] = vwap
                vs_vwap = round(((current - vwap) / vwap) * 100, 1)
                result["vs_vwap"]  = vs_vwap
                result["vwap_label"] = (
                    "Above 20d VWAP +" + str(vs_vwap) + "% 🟢"
                    if vs_vwap >= 0 else
                    "Below 20d VWAP " + str(vs_vwap) + "% 🔴"
                )

        # ── Support & Resistance (20-day high/low) ────────────────────
        hi_20  = round(max(highs[-20:]), 2)
        lo_20  = round(min(lows[-20:]), 2)
        range_20 = hi_20 - lo_20
        result["hi_20d"] = hi_20
        result["lo_20d"] = lo_20
        if range_20 > 0:
            pct_in_range = round(((current - lo_20) / range_20) * 100, 0)
            result["range_position"] = pct_in_range
            if pct_in_range >= 80:
                result["range_label"] = "Near 20d high — breakout zone"
            elif pct_in_range <= 20:
                result["range_label"] = "Near 20d low — support zone"
            else:
                result["range_label"] = "Mid-range"

        # ── 30-day trend ──────────────────────────────────────────────
        if len(closes) >= 30:
            start_30 = closes[-30]
            pct_30d  = round(((current - start_30) / start_30) * 100, 1)
            result["pct_30d"] = pct_30d
            result["trend_30d"] = (
                "Strong uptrend 🟢" if pct_30d > 15 else
                "Uptrend 🟢" if pct_30d > 5 else
                "Flat ⚪" if pct_30d > -5 else
                "Downtrend 🔴" if pct_30d > -15 else
                "Strong downtrend 🔴"
            )

        # ── Golden/Death cross ────────────────────────────────────────
        if ma50 and ma200:
            if ma50 > ma200:
                result["cross_label"] = "Golden cross (50MA > 200MA) 🟢"
            else:
                result["cross_label"] = "Death cross (50MA < 200MA) 🔴"

        print(f"[SENTIMENT] Technical: RSI={result.get('rsi')} "
              f"vs50MA={result.get('vs_ma50')}% "
              f"30d={result.get('pct_30d')}%")
        return result

    except Exception as e:
        print(f"[SENTIMENT] Technical error: {e}")
        return {}

# ── Options Flow (from our own history) ───────────────────────────────

def fetch_flow_sentiment(ticker: str) -> dict:
    """Check today's flow alerts for this ticker from our own history."""
    result = {}
    try:
        from flow_intelligence import load_flow_history
        history = load_flow_history()
        today   = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")

        today_flows = [
            f for f in history
            if f.get("ticker","").upper() == ticker.upper()
            and f.get("date") == today
        ]

        if today_flows:
            calls = [f for f in today_flows
                     if f.get("option_type","call") == "call"]
            puts  = [f for f in today_flows
                     if f.get("option_type","call") == "put"]
            total_prem = sum(
                int(f.get("premium",0) or 0) for f in today_flows
            )
            prem_str = (f"${total_prem/1_000_000:.1f}M"
                        if total_prem >= 1_000_000
                        else f"${total_prem/1000:.0f}K")

            pc_ratio = round(len(puts)/len(calls), 2) if calls else None

            if len(calls) > len(puts):
                flow_bias = "Bullish 🟢"
            elif len(puts) > len(calls):
                flow_bias = "Bearish 🔴"
            else:
                flow_bias = "Neutral ⚪"

            result.update({
                "flow_count":    len(today_flows),
                "call_count":    len(calls),
                "put_count":     len(puts),
                "total_premium": prem_str,
                "pc_ratio":      pc_ratio,
                "flow_bias":     flow_bias,
                "verdicts":      [f.get("verdict","?") for f in today_flows],
            })
    except Exception as e:
        print(f"[SENTIMENT] Flow error: {e}")
    return result

# ── Overall Sentiment Score ────────────────────────────────────────────

def calc_overall_sentiment(price: dict, news: dict,
                            insider: dict, flow: dict,
                            tech: dict = None) -> str:
    """Calculate overall sentiment label from all signals."""
    bull = 0
    bear = 0

    # Price momentum
    pct = price.get("pct_change", 0) or 0
    if pct > 2:   bull += 2
    elif pct > 0: bull += 1
    elif pct < -2: bear += 2
    elif pct < 0:  bear += 1

    # News sentiment
    sent = news.get("sentiment","")
    if sent == "Bullish": bull += 2
    elif sent == "Bearish": bear += 2

    # Insider
    ins_label = insider.get("insider_label","")
    if "buying" in ins_label: bull += 2
    elif "selling" in ins_label: bear += 1

    # Analyst
    ab = insider.get("analyst_buy", 0)
    as_ = insider.get("analyst_sell", 0)
    at  = insider.get("analyst_total", 1)
    if at > 0:
        if ab/at > 0.6: bull += 1
        elif as_/at > 0.4: bear += 1

    # Options flow
    fb = flow.get("flow_bias","")
    if "Bullish" in fb: bull += 2
    elif "Bearish" in fb: bear += 2

    # Technical signals
    if tech:
        mas_above = tech.get("mas_above", 0)
        mas_total = tech.get("mas_total", 1)
        rsi       = tech.get("rsi", 50)
        pct_30d   = tech.get("pct_30d", 0) or 0
        vs_ma50   = tech.get("vs_ma50", 0) or 0

        # MA positioning
        if mas_total > 0:
            ratio = mas_above / mas_total
            if ratio >= 0.75:   bull += 2
            elif ratio >= 0.5:  bull += 1
            elif ratio <= 0.25: bear += 2
            elif ratio <= 0.5:  bear += 1

        # RSI
        if rsi >= 70:   bear += 1  # Overbought — may pull back
        elif rsi >= 55: bull += 1
        elif rsi <= 30: bull += 1  # Oversold — bounce potential
        elif rsi <= 45: bear += 1

        # 30-day trend
        if pct_30d > 10:   bull += 1
        elif pct_30d < -10: bear += 1

    if bull >= 6:    return "STRONGLY BULLISH 🟢🟢"
    elif bull >= 4:  return "BULLISH 🟢"
    elif bull >= 2 and bull > bear: return "CAUTIOUSLY BULLISH ⚠️🟢"
    elif bear >= 6:  return "STRONGLY BEARISH 🔴🔴"
    elif bear >= 4:  return "BEARISH 🔴"
    elif bear >= 2 and bear > bull: return "CAUTIOUSLY BEARISH ⚠️🔴"
    else:            return "NEUTRAL ⚪"

# ── Master Function ────────────────────────────────────────────────────

def get_sentiment(ticker: str) -> str:
    """
    Full sentiment analysis for a ticker.
    Returns formatted Telegram message.
    """
    ticker  = ticker.upper()
    now_et  = datetime.now(ZoneInfo("America/New_York"))

    print(f"[SENTIMENT] Fetching for {ticker}...")

    # Note market status
    from market_calendar import market_status as _mstatus
    mkt = _mstatus()

    # Fetch all data — no Polygon calls
    price   = fetch_price_data(ticker)
    volume  = fetch_volume_data(ticker)
    news    = fetch_news_sentiment(ticker)
    insider = fetch_insider_sentiment(ticker)
    flow    = fetch_flow_sentiment(ticker)
    tech    = fetch_technical_analysis(ticker)

    # Overall sentiment — include technical signals
    overall = calc_overall_sentiment(price, news, insider, flow, tech)

    market_note = ""
    if not mkt["is_open"]:
        market_note = chr(10) + mkt["emoji"] + " " + mkt["label"] + " — using last available data"

    lines = [
        "📊 " + ticker + " Sentiment — " + now_et.strftime("%b %d %I:%M%p ET") + market_note,
        "",
    ]

    # Price
    if price.get("price"):
        pct        = price.get("pct_change", 0)
        date_label = price.get("price_date_label", "today")
        context    = price.get("price_context", "")
        lines.append(
            price.get("price_emoji","") + " Price: $" + str(price["price"]) +
            context + " as of " + date_label +
            " (" + f"{pct:+.2f}%" + " vs prev close)"
        )
        if price.get("high") and price.get("low"):
            lines.append(
                "   " + date_label + " range: $" + str(price["low"]) +
                " — $" + str(price["high"])
            )

    # Volume
    if volume.get("vol_ratio"):
        lines.append(
            f"📊 Volume: {volume['vol_ratio']}x average — "
            f"{volume.get('vol_label','')}"
        )

    lines.append("")

    # News
    sent_emoji = news.get("sentiment_emoji","")
    sent_label = news.get("sentiment") or "No sentiment data"
    if not sent_label or sent_label == "None":
        sent_label = "No sentiment data"
        sent_emoji = ""
    lines.append("📰 News sentiment: " + sent_label + (" " + sent_emoji if sent_emoji else ""))
    if news.get("bull_pct") is not None and (news["bull_pct"] > 0 or news.get("bear_pct",0) > 0):
        lines.append(
            "   Bull " + str(news["bull_pct"]) + "%" +
            " | Bear " + str(news.get("bear_pct",0)) + "%" +
            " | " + str(news.get("articles_week",0)) + " articles/week"
        )
    elif news.get("article_count",0) > 0:
        lines.append("   " + str(news.get("article_count",0)) + " articles in last 24h")
    if news.get("articles"):
        for a in news["articles"][:2]:
            h = (a.get("headline","") or "").strip()[:80]
            if h:
                lines.append("   · " + h)

    lines.append("")

    # Options flow
    if flow.get("flow_count"):
        lines.append(
            f"⚡ Options flow today: {flow['flow_bias']}"
        )
        lines.append(
            f"   {flow['call_count']} calls | {flow['put_count']} puts "
            f"| {flow['total_premium']} total"
        )
        if flow.get("pc_ratio") is not None:
            lines.append(
                f"   P/C ratio: {flow['pc_ratio']} "
                f"({'heavy calls' if flow['pc_ratio'] < 0.5 else 'balanced' if flow['pc_ratio'] < 1.5 else 'heavy puts'})"
            )
    else:
        lines.append("⚡ Options flow today: No FlowCheck alerts")

    lines.append("")

    # Insider + analyst
    if insider.get("insider_label"):
        lines.append(
            f"👔 Insiders (90d): {insider['insider_label']}"
        )
        lines.append(
            f"   {insider.get('insider_buys',0)} buys | "
            f"{insider.get('insider_sells',0)} sells"
        )
    else:
        lines.append("👔 Insiders: No activity last 90 days")

    if insider.get("analyst_total"):
        lines.append(
            f"🏦 Analysts ({insider['analyst_period']}): "
            f"{insider.get('analyst_buy',0)} buy | "
            f"{insider.get('analyst_hold',0)} hold | "
            f"{insider.get('analyst_sell',0)} sell "
            f"(of {insider['analyst_total']})"
        )

    lines.append("")
    lines.append(f"Overall: {overall}")

    return "\n".join(lines)
