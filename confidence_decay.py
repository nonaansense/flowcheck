"""
confidence_decay.py — Time-based conviction decay for FlowCheck.

A flow alert ages. If no technical confirmation materialises and
the stock hasn't moved toward the strike, conviction should decrease.

Decay schedule (applied to conviction total):
  Day 0-1:  no decay
  Day 2-3:  -0.5
  Day 4-5:  -1.0
  Day 6-7:  -1.5
  Day 8+:   -2.0 (stale)

Additional penalties:
  - Stock moved >3% AWAY from strike since flow:  -0.5
  - No technical confirmation after 3 days:       -0.5
"""
import time
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def apply_decay(
    conv: dict,
    watch_entry: dict,
    current_price: float,
) -> dict:
    """
    Apply time-based and context-based decay to conviction dict.
    Returns updated conviction dict with decay_note added.
    """
    if not conv or not watch_entry:
        return conv

    added_at   = float(watch_entry.get("added_at", time.time()) or time.time())
    days_held  = (time.time() - added_at) / 86400
    is_call    = "put" not in (watch_entry.get("option_type","call") or "call").lower()
    strike_f   = float(watch_entry.get("strike", 0) or 0)
    entry_px   = float(watch_entry.get("stock_price_at_alert", current_price) or current_price)

    decay      = 0.0
    decay_notes = []

    # Time decay
    if days_held >= 8:
        decay += 2.0
        decay_notes.append("⏰ 8+ days old — stale")
    elif days_held >= 6:
        decay += 1.5
        decay_notes.append("⏰ 6-7 days old")
    elif days_held >= 4:
        decay += 1.0
        decay_notes.append("⏰ 4-5 days old")
    elif days_held >= 2:
        decay += 0.5
        decay_notes.append("⏰ 2-3 days old")

    # Stock moved AWAY from strike since flow
    if strike_f > 0 and current_price > 0 and entry_px > 0:
        if is_call:
            # Bad: stock dropped since flow
            move_pct = (current_price - entry_px) / entry_px * 100
            if move_pct < -3:
                decay += 0.5
                decay_notes.append(f"📉 Stock {abs(move_pct):.1f}% lower since flow")
        else:
            # Bad: stock rose since flow
            move_pct = (current_price - entry_px) / entry_px * 100
            if move_pct > 3:
                decay += 0.5
                decay_notes.append(f"📈 Stock {move_pct:.1f}% higher since flow (put thesis weakening)")

    # No technical confirmation after 3 days
    if days_held >= 3 and not conv.get("scores",{}).get("technical", False):
        decay += 0.5
        decay_notes.append("📊 No technical confirmation after 3 days")

    if decay <= 0:
        return conv

    # Apply decay
    new_total = max(0, conv["total"] - decay)

    if new_total >= 5:   label = "🔥 ELITE"
    elif new_total >= 4: label = "💎 HIGH"
    elif new_total >= 3: label = "✅ MODERATE"
    elif new_total >= 2: label = "⚠️ LOW"
    else:                label = "❌ SKIP"

    updated = dict(conv)
    updated["total"]       = round(new_total, 1)
    updated["label"]       = label
    updated["decay"]       = round(decay, 1)
    updated["decay_notes"] = decay_notes
    updated["days_held"]   = round(days_held, 1)

    return updated


def format_decay_note(conv: dict) -> str:
    """One-line decay summary if decay was applied."""
    decay = conv.get("decay", 0)
    if not decay:
        return ""
    notes = conv.get("decay_notes", [])
    days  = conv.get("days_held", 0)
    return (f"⏳ Conviction decay -{decay:.1f} "
            f"(Day {int(days)+1}: {' · '.join(notes[:2])})")
