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
                    related  = [r.lower() for r in (a.get("related","") or "").split(",")]

                    # Must contain ticker symbol OR company name in headline
                    company_names = {
                        "DELL": ["dell"], "MSFT": ["microsoft"], "AAPL": ["apple"],
                        "NVDA": ["nvidia"], "TSLA": ["tesla"], "AMZN": ["amazon"],
                        "META": ["meta", "facebook"], "GOOGL": ["google", "alphabet"],
                        "ORCL": ["oracle"], "CRM": ["salesforce"], "AMD": ["amd"],
                        "INTC": ["intel"], "BLDP": ["ballard"], "FLNC": ["fluence"],
                        "NOK": ["nokia"], "ASTS": ["ast spacemobile"],
                    }
                    names = company_names.get(ticker.upper(), [ticker.lower()])
                    mentions = any(n in headline for n in names + [ticker.lower()])

                    if not mentions:
                        continue

                    # Skip "top stocks" list articles — too generic
                    list_keywords = ["top ", "best ", "watch list", "stocks to watch",
                                     "3 stocks", "4 stocks", "5 stocks", "buy now"]
                    is_list_article = any(kw in headline for kw in list_keywords)
                    if is_list_article:
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
        articles = news_6h[:2]
        result["news_articles"]   = [{"headline": a.get("headline","")[:100], "url": a.get("url","")} for a in articles]
        result["news_summary"]    = " | ".join([a.get("headline","")[:100] for a in articles])
        result["news_emoji"]      = "📰"
        result["flow_context"]    = "Recent news may explain flow — lower conviction signal"
        result["flow_context_emoji"] = "⚠️"
    elif news_24h:
        result["has_recent_news"] = True
        result["news_count"]      = len(news_24h)
        articles = news_24h[:2]
        result["news_articles"]   = [{"headline": a.get("headline","")[:100], "url": a.get("url","")} for a in articles]
        result["news_summary"]    = " | ".join([a.get("headline","")[:100] for a in articles])
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
    """Format news context for Telegram message with article links."""
    lines = []

    if news_data.get("flow_context"):
        emoji = news_data.get("flow_context_emoji","")
        lines.append(f"{emoji} {news_data['flow_context']}")

    # Show each article as a clickable link
    articles = news_data.get("news_articles",[])
    if articles:
        for a in articles[:2]:
            headline = a.get("headline","")[:80]
            url      = a.get("url","")
            if url:
                lines.append(f'📰 <a href="{url}">{headline}</a>')
            else:
                lines.append(f"📰 {headline}")
    elif news_data.get("news_summary"):
        lines.append(f"📰 {news_data['news_summary'][:120]}")

    if news_data.get("insider_summary"):
        lines.append(f"👔 Insiders: {news_data['insider_summary'][:80]}")

    return lines


def fetch_google_news(ticker: str, hours: int = 24, max_results: int = 5) -> list:
    """
    Fetch recent news from Google News RSS — broader coverage than Finnhub's
    curated company-news feed, often catches breaking news, analyst notes,
    and rumors faster. No API key required.

    Returns list of {headline, source, url, datetime} matching the same
    shape as fetch_recent_news() for drop-in compatibility.
    """
    import xml.etree.ElementTree as ET
    from urllib.parse import quote

    try:
        query = quote(f"{ticker} stock")
        url   = f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
        r = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return []

        root  = ET.fromstring(r.content)
        items = root.findall(".//item")
        cutoff = time.time() - hours * 3600

        articles = []
        for item in items:
            title_el  = item.find("title")
            link_el   = item.find("link")
            date_el   = item.find("pubDate")
            source_el = item.find("source")
            if title_el is None or date_el is None:
                continue

            try:
                from email.utils import parsedate_to_datetime
                pub_dt = parsedate_to_datetime(date_el.text)
                pub_ts = pub_dt.timestamp()
            except Exception:
                continue

            if pub_ts < cutoff:
                continue

            headline = title_el.text or ""
            # Google News titles often end in " - SourceName"; strip if source tag missing
            source = source_el.text if source_el is not None else ""
            if not source and " - " in headline:
                headline, _, source = headline.rpartition(" - ")

            articles.append({
                "headline": headline.strip(),
                "source":   source.strip(),
                "url":      link_el.text if link_el is not None else "",
                "datetime": pub_ts,
            })

        articles.sort(key=lambda a: a["datetime"], reverse=True)
        return articles[:max_results]

    except Exception as e:
        print(f"[NEWS] Google News RSS error for {ticker}: {e}")
        return []


def fetch_combined_news(ticker: str, hours: int = 48, max_results: int = 3) -> list:
    """
    Merge Finnhub company-news with Google News RSS for broader coverage.
    Deduplicates by headline similarity. Use this instead of calling
    fetch_recent_news() and fetch_google_news() separately.
    """
    finnhub_articles = fetch_recent_news(ticker, hours=hours)
    google_articles  = fetch_google_news(ticker, hours=hours, max_results=max_results)

    seen_headlines = set()
    combined = []
    for art in finnhub_articles + google_articles:
        # Simple dedup: first 40 chars lowercased
        sig = (art.get("headline","") or "")[:40].lower().strip()
        if sig and sig not in seen_headlines:
            seen_headlines.add(sig)
            combined.append(art)

    combined.sort(key=lambda a: a.get("datetime", 0), reverse=True)
    return combined[:max_results]
