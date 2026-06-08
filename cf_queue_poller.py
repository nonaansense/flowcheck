"""
cf_queue_poller.py — Polls Cloudflare Worker queue for buffered FlowGod tweets.

When Railway has a networking incident, IFTTT delivers to Cloudflare instead.
This poller runs every 60s and processes any queued tweets.

Railway env vars needed:
  CF_WORKER_URL    = https://your-worker.workers.dev
  CF_WORKER_SECRET = your_shared_secret (same as AUTH_SECRET in Worker)
"""
import os, time, json, requests
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

_last_poll_ids: set = set()   # prevent double-processing in same session


def poll_cf_queue(process_fn=None) -> int:
    """
    Poll Cloudflare Worker queue and process any pending tweets.
    process_fn: async callable(tweet, tweet_url) — same as webhook handler.
    Returns number of items processed.
    """
    worker_url = os.environ.get("CF_WORKER_URL","").rstrip("/")
    secret     = os.environ.get("CF_WORKER_SECRET","")
    if not worker_url or not secret:
        return 0

    try:
        r = requests.get(
            f"{worker_url}/queue",
            params={"secret": secret},
            timeout=8
        )
        if r.status_code != 200:
            print(f"[CF_QUEUE] Poll error: {r.status_code}")
            return 0

        data  = r.json()
        items = data.get("items", [])
        if not items:
            return 0

        print(f"[CF_QUEUE] {len(items)} pending tweet(s) in buffer")
        processed = 0

        for item in items:
            item_id   = item.get("id","")
            tweet     = item.get("tweet","")
            tweet_url = item.get("tweet_url","")
            queued_at = item.get("queued_at","")

            if not tweet or item_id in _last_poll_ids:
                continue

            # Skip items older than 4 hours (stale flow)
            try:
                q_ts  = datetime.fromisoformat(queued_at).timestamp()
                age_h = (time.time() - q_ts) / 3600
                if age_h > 4:
                    print(f"[CF_QUEUE] Skipping stale item {item_id} ({age_h:.1f}h old)")
                    _ack(worker_url, secret, item_id)
                    continue
            except: pass

            print(f"[CF_QUEUE] Processing buffered tweet: {tweet[:60]}")

            # Process via the same pipeline as direct webhooks
            if process_fn:
                try:
                    import asyncio
                    asyncio.create_task(process_fn(tweet, tweet_url))
                except:
                    try:
                        loop = asyncio.get_event_loop()
                        loop.create_task(process_fn(tweet, tweet_url))
                    except Exception as e:
                        print(f"[CF_QUEUE] Process error: {e}")

            # Acknowledge
            _ack(worker_url, secret, item_id)
            _last_poll_ids.add(item_id)
            processed += 1

        return processed

    except Exception as e:
        print(f"[CF_QUEUE] Poll exception: {e}")
        return 0


def _ack(worker_url: str, secret: str, item_id: str):
    """Mark item as processed in Cloudflare KV."""
    try:
        requests.post(
            f"{worker_url}/ack",
            params={"secret": secret},
            json={"id": item_id},
            timeout=5
        )
    except: pass


def get_queue_status() -> dict:
    """Check queue status — for /test command."""
    worker_url = os.environ.get("CF_WORKER_URL","").rstrip("/")
    secret     = os.environ.get("CF_WORKER_SECRET","")
    if not worker_url or not secret:
        return {"configured": False}
    try:
        r = requests.get(f"{worker_url}/status",
                         params={"secret": secret}, timeout=5)
        if r.status_code == 200:
            data = r.json()
            return {"configured": True, **data}
    except: pass
    return {"configured": True, "error": "unreachable"}
