"""
targeted_selftest.py — end-to-end validation of the targeted-strikes pipeline.

Runs synthetic alerts through the ENTIRE chain in a few seconds:

    score → factor capture → pool write → pool read → factor analysis

so a broken link is caught before hours of replay time are spent on it.

Every check here corresponds to a bug that actually shipped and cost a
re-run:
  • pool stored as a raw list while storage.db_set takes a string, so the
    pool silently reset to empty on every read
  • alerts flagged untradeable were dropped before their factors were
    recorded, deleting the contrast group for the very factors under test
  • the "room to next level" factor fired on ~4% of charts because every
    minor wiggle counted as a level

Telegram: /targeted_selftest
"""
import json
import random


def _mock_storage():
    """Storage that behaves like Supabase: STRINGS ONLY, both directions."""
    store = {}

    def db_get(key):
        v = store.get(key)
        if v is not None and not isinstance(v, str):
            raise AssertionError(
                f"db_get returned {type(v).__name__}, not str — "
                f"storage only holds strings")
        return v

    def db_set(key, value):
        if not isinstance(value, str):
            raise AssertionError(
                f"db_set got {type(value).__name__}, not str — "
                f"values must be json.dumps()'d first")
        store[key] = value
        return True

    return db_get, db_set, store


def _synthetic_alerts(n: int = 300, seed: int = 5) -> list:
    """Alerts shaped like real scored output, including untradeable ones."""
    random.seed(seed)
    import factor_lab as FL
    out = []
    for i in range(n):
        facs = {f: random.random() < 0.5 for f in FL.FACTOR_LABELS}
        won = random.random() < (0.55 if facs["30m_trend_aligned"] else 0.25)
        out.append({
            "date": f"2026-{((i // 60) % 12) + 1:02d}-{(i % 28) + 1:02d}",
            "time": "15:00:00",
            "ticker": f"T{i}", "strike": "100", "expiry": "07/17/26",
            "direction": "call" if i % 4 else "put",
            "count": 4 + (i % 5), "total_prem": 100000 * (1 + i % 8),
            "factors": facs,
            # every 3rd alert is untradeable — these MUST still be analysed
            "swing_dq": "daily regime opposed the trade" if i % 3 == 0 else None,
            "swing_score": random.randint(20, 90),
            "pricing": {"max_gain_pct": random.uniform(60, 400) if won
                        else random.uniform(0, 40),
                        "expiry_pct": -60.0},
        })
    return out


def run_selftest() -> list:
    """Returns report lines. Every FAIL is a bug that would waste a backtest."""
    lines = ["🧪 TARGETED PIPELINE SELF-TEST", ""]
    passed = failed = 0

    def check(name, cond, detail=""):
        nonlocal passed, failed
        if cond:
            passed += 1
            lines.append(f"  ✅ {name}")
        else:
            failed += 1
            lines.append(f"  ❌ {name}{(' — ' + detail) if detail else ''}")

    import sys, types
    db_get, db_set, store = _mock_storage()
    real_storage = sys.modules.get("storage")
    sys.modules["storage"] = types.SimpleNamespace(db_get=db_get, db_set=db_set)

    try:
        import importlib
        import factor_lab as FL
        importlib.reload(FL)

        alerts = _synthetic_alerts()

        # 1. Untradeable alerts must survive into the analysis sample.
        usable = FL._usable(alerts)
        n_dq = sum(1 for a in alerts if a.get("swing_dq"))
        check("untradeable alerts kept for analysis",
              len(usable) == len(alerts),
              f"{len(usable)}/{len(alerts)} usable, {n_dq} were flagged dq")

        # 2. Pool write must serialise correctly against string-only storage.
        try:
            n1 = FL.save_to_pool(alerts[:150])
            wrote = True
        except AssertionError as e:
            n1, wrote = 0, False
            lines.append(f"     storage rejected the write: {e}")
        check("pool write uses string serialisation", wrote and n1 == 150,
              f"wrote {n1}")

        # 3. Pool must actually persist and ACCUMULATE, not reset.
        n2 = FL.save_to_pool(alerts[150:])
        check("pool accumulates across runs", n2 == 300, f"got {n2} after 2 runs")

        # 4. Round-trip must preserve factors.
        pooled = FL.load_pool()
        has_facs = all(p.get("factors") for p in pooled)
        check("factors survive the storage round-trip",
              len(pooled) == 300 and has_facs,
              f"{len(pooled)} loaded")

        # 5. Dedup — re-running a month must not double-count.
        n3 = FL.save_to_pool(alerts)
        check("re-running a month dedupes", n3 == 300, f"got {n3}")

        # 6. Analysis runs and detects the planted edge.
        rep = FL.run_factor_lab(pooled)
        found = any("SURVIVES" in l for l in rep)
        # n=300 is deliberate: at n=60 the harness can only resolve ~47pp,
        # so a 30pp edge SHOULD be missed and the test would fail on a
        # correctly-working system.
        check("analysis detects a planted edge (n=300)", found,
              "planted +30pp on 30m_trend_aligned but nothing surfaced")

        # 7. Power note present — a null result must be interpretable.
        check("power/MDE reported", any("CAN RESOLVE" in l for l in rep))

        # 8. Level detection must fire at a usable rate, not ~0.
        try:
            import targeted_swing_backtest_score as SB
            hits = 0
            for seed in range(60):
                random.seed(seed)
                bars, p = [], 100.0
                for _ in range(130):
                    p += random.gauss(0, 0.35)
                    bars.append({"high": p + 0.3, "low": p - 0.3, "close": p})
                px = bars[-1]["close"]
                rl, _sl = SB._levels(bars)
                nr, _t = SB._nearest_level(rl, px, above=True)
                if nr and (nr - px) / px * 100 >= 2.0:
                    hits += 1
            rate = hits / 60 * 100
            check(f"'2%+ room' factor fires at a testable rate ({rate:.0f}%)",
                  8 <= rate <= 60,
                  f"{rate:.0f}% — a factor that never fires can't be tested")
        except Exception as e:
            check("level detection", False, str(e))

        # 9. Clearing works.
        FL.clear_pool()
        check("pool clear works", FL.load_pool() == [])

    except Exception as e:
        failed += 1
        lines.append(f"  ❌ selftest crashed: {type(e).__name__}: {e}")
    finally:
        if real_storage is not None:
            sys.modules["storage"] = real_storage
        else:
            sys.modules.pop("storage", None)

    lines += ["", f"{passed} passed, {failed} failed"]
    if failed:
        lines.append("⚠️ Do NOT start a backtest run — fix these first.")
    else:
        lines.append("✅ Pipeline is sound. Safe to run backtests.")
    return lines
