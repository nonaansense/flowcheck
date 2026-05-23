"""
News check for FlowCheck.
Fetches recent news for a ticker using Finnhub.
Determines if flow is ahead of public news or unexplained.
"""
import os, requests, time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

def fh_key():
    return os.environ.get("FINNHUB_API_KEY")

def fetch_recent_news(ticker: str, hours: int = 24) -> list:
    """Fetch recent news articles for ticker from Finnhub."""
    key = fh_key()
    if not key:
        return []
    try:
        to_dt   = datetime.now()
        from_dt = to_dt - timedelta(hours=hours)
        r = requests.get(
            "https://finnhub.io/api/v1/company-news",
            params={
                "symbol": ticker.upper(),
                "from":   from_dt.strftime("%Y-%m-%d"),
                "to":     to_dt.strftime("%Y-%m-%d"),
                "token":  key
            },
            timeout=8
        )
        if r.status_code == 200:
            articles = r.json()
            if isinstance(articles, list):
                cutoff = time.time() - hours * 3600
                # Filter to recent AND ticker-specific (exclude generic market headlines)
                generic_keywords = [
                    "dow jones", "s&p 500", "nasdaq", "market futures",
                    "stock market", "wall street", "fed ", "federal reserve",
                    "interest rate", "inflation", "gdp", "jobs report",
                ]
                recent = []
                for a in articles:
                    if a.get("datetime", 0) < cutoff:
                        continue
                    headline = (a.get("headline","") or "").lower()
                    # Skip if headline is generic market news not mentioning ticker
                    is_generic = any(kw in headline for kw in generic_keywords)
                    mentions_ticker = ticker.lower() in headline
                    if is_generic and not mentions_ticker:
                        continue
                    recent.append(a)
                print(f"[NEWS] {ticker}: {len(recent)} ticker-specific articles in last {hours}h")
                return recent[:5]
    except Exception as e:
        print(f"[NEWS] Error for {ticker}: {e}")
    return []

def fetch_insider_filings(ticker: str) -> list:
    """Fetch recent SEC Form 4 insider transactions from Finnhub."""
    key = fh_key()
    if not key:
        return []
    try:
        r = requests.get(
            "https://finnhub.io/api/v1/stock/insider-transactions",
            params={"symbol": ticker.upper(), "token": key},
            timeout=8
        )
        if r.status_code == 200:
            data = r.json()
            txns = data.get("data", [])
            # Filter to buys only in last 30 days
            cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
            buys   = [
                t for t in txns
                if t.get("transactionType","") in ("Buy","P - Purchase")
                and t.get("transactionDate","") >= cutoff
                and float(t.get("share",0) or 0) > 0
            ]
            print(f"[NEWS] {ticker}: {len(buys)} insider buys in last 30d")
            return buys[:3]
    except Exception as e:
        print(f"[NEWS] Insider error for {ticker}: {e}")
    return []

def analyze_news_context(ticker: str) -> dict:
    """
    Full news context analysis.
    Returns summary of news + insider activity.
    """
    result = {
        "has_recent_news":      False,
        "news_count":           0,
        "news_summary":         None,
        "news_emoji":           "",
        "has_insider_buying":   False,
        "insider_summary":      None,
        "flow_context":         None,
        "flow_context_emoji":   "",
    }

    # Recent news (last 6 hours = very recent)
    news_6h  = fetch_recent_news(ticker, hours=6)
    news_24h = fetch_recent_news(ticker, hours=24)

    if news_6h:
        result["has_recent_news"] = True
        result["news_count"]      = len(news_6h)
        headlines = [a.get("headline","")[:60] for a in news_6h[:2]]
        result["news_summary"]    = " | ".join(headlines)
        result["news_emoji"]      = "📰"
        result["flow_context"]    = "Recent news may explain flow — lower conviction signal"
        result["flow_context_emoji"] = "⚠️"
    elif news_24h:
        result["has_recent_news"] = True
        result["news_count"]      = len(news_24h)
        headlines = [a.get("headline","")[:60] for a in news_24h[:2]]
        result["news_summary"]    = " | ".join(headlines)
        result["news_emoji"]      = "📰"
        result["flow_context"]    = "News in last 24h — flow may be news-driven"
        result["flow_context_emoji"] = "⚠️"
    else:
        result["flow_context"]       = "No recent news — flow appears ahead of unknown catalyst 🔍"
        result["flow_context_emoji"] = "✅"

    # Insider buying
    insider_buys = fetch_insider_filings(ticker)
    if insider_buys:
        total_shares = sum(float(t.get("share",0) or 0) for t in insider_buys)
        names        = list(set(t.get("name","?") for t in insider_buys[:2]))
        result["has_insider_buying"] = True
        result["insider_summary"]    = (
            f"{len(insider_buys)} insider buy(s) last 30d — "
            f"{', '.join(names)} — {total_shares:,.0f} shares total"
        )

    return result

def format_news_for_sms(news_data: dict) -> list:
    """Format news context for Telegram message."""
    lines = []

    if news_data.get("flow_context"):
        emoji = news_data.get("flow_context_emoji","")
        lines.append(f"{emoji} {news_data['flow_context']}")

    if news_data.get("news_summary"):
        lines.append(f"📰 News: {news_data['news_summary'][:80]}")

    if news_data.get("insider_summary"):
        lines.append(f"👔 Insiders: {news_data['insider_summary'][:80]}")

    return lines
