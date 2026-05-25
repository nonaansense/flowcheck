"""
Persistent storage for FlowCheck.

Uses Supabase REST API (no direct Postgres port needed — works on Railway).
Falls back to /tmp if not configured.

Required Railway environment variables:
  SUPABASE_URL = https://iczaezmcrueskbheyenx.supabase.co
  SUPABASE_KEY = your-anon-key (from Supabase Settings > API)
"""
import os, json, requests

def get_supabase_url() -> str | None:
    return os.environ.get("SUPABASE_URL","").rstrip("/") or None

def get_supabase_key() -> str | None:
    return os.environ.get("SUPABASE_KEY","") or None

def has_db() -> bool:
    return bool(get_supabase_url() and get_supabase_key())

def _headers() -> dict:
    return {
        "apikey":        get_supabase_key(),
        "Authorization": "Bearer " + get_supabase_key(),
        "Content-Type":  "application/json",
        "Prefer":        "return=representation",
    }

def _base() -> str:
    return get_supabase_url() + "/rest/v1/flowcheck_store"

_db_initialized = False

def init_db():
    """Verify Supabase REST API is reachable. Table must exist in Supabase."""
    global _db_initialized
    if _db_initialized:
        return
    if not has_db():
        print("[STORAGE] ⚠️  No SUPABASE_URL/SUPABASE_KEY — data lost on redeploy!")
        _db_initialized = True
        return
    try:
        r = requests.get(
            _base() + "?key=eq.__ping__&select=key",
            headers=_headers(),
            timeout=8
        )
        if r.status_code in (200, 406):
            _db_initialized = True
            print("[STORAGE] ✅ Supabase REST API connected — data persists across redeploys")
        else:
            print(f"[STORAGE] ⚠️  Supabase ping failed: {r.status_code} {r.text[:100]}")
            print("[STORAGE] Make sure the flowcheck_store table exists in Supabase")
            _db_initialized = True
    except Exception as e:
        print(f"[STORAGE] ⚠️  Supabase connection failed: {e}")
        _db_initialized = True

def ensure_table():
    """
    Create the storage table in Supabase if it doesn't exist.
    Run this once via /migrate endpoint.
    Uses Supabase SQL API.
    """
    url = get_supabase_url()
    key = get_supabase_key()
    if not url or not key:
        return "No Supabase credentials"
    try:
        r = requests.post(
            url + "/rest/v1/rpc/exec_sql",
            headers={
                "apikey":        key,
                "Authorization": "Bearer " + key,
                "Content-Type":  "application/json",
            },
            json={"sql": """
                CREATE TABLE IF NOT EXISTS flowcheck_store (
                    key     TEXT PRIMARY KEY,
                    value   TEXT NOT NULL,
                    updated TIMESTAMPTZ DEFAULT NOW()
                );
            """},
            timeout=10
        )
        return f"Table creation: {r.status_code}"
    except Exception as e:
        return f"Table creation error: {e}"

def db_get(key: str) -> str | None:
    """Read value from Supabase via REST API."""
    if not has_db():
        return None
    try:
        r = requests.get(
            _base() + f"?key=eq.{key}&select=value",
            headers=_headers(),
            timeout=8
        )
        if r.status_code == 200:
            rows = r.json()
            if rows:
                val = rows[0].get("value")
                print(f"[STORAGE] ✅ Loaded '{key}' from Supabase ({len(val)} chars)")
                return val
        print(f"[STORAGE] '{key}' not found in Supabase (status {r.status_code})")
        return None
    except Exception as e:
        print(f"[STORAGE] ⚠️  db_get({key}) error: {e}")
        return None

def db_set(key: str, value: str) -> bool:
    """Write value to Supabase via REST API (upsert)."""
    if not has_db():
        return False
    try:
        r = requests.post(
            _base(),
            headers={**_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
            json={"key": key, "value": value},
            timeout=10
        )
        if r.status_code in (200, 201, 204):
            print(f"[STORAGE] ✅ Saved '{key}' to Supabase ({len(value)} chars)")
            return True
        print(f"[STORAGE] ⚠️  db_set({key}) failed: {r.status_code} {r.text[:120]}")
        return False
    except Exception as e:
        print(f"[STORAGE] ⚠️  db_set({key}) error: {e} — NOT saved!")
        return False

def load_data(key: str, tmp_path: str, default) -> dict | list:
    """Load from Supabase first, /tmp fallback, then default."""
    init_db()

    if has_db():
        raw = db_get(key)
        if raw:
            try:
                return json.loads(raw)
            except Exception as e:
                print(f"[STORAGE] JSON parse error ({key}): {e}")

    try:
        with open(tmp_path) as f:
            data = json.load(f)
        print(f"[STORAGE] Loaded '{key}' from /tmp (Supabase miss)")
        return data
    except:
        pass

    print(f"[STORAGE] '{key}' not found anywhere — using default")
    return default

def save_data(key: str, tmp_path: str, data) -> bool:
    """Save to Supabase AND /tmp backup."""
    init_db()
    json_str = json.dumps(data)
    db_ok    = False

    if has_db():
        db_ok = db_set(key, json_str)
        if not db_ok:
            print(f"[STORAGE] ⚠️  '{key}' NOT saved to Supabase — will be lost on redeploy!")

    try:
        with open(tmp_path, "w") as f:
            f.write(json_str)
    except Exception as e:
        print(f"[STORAGE] /tmp save error: {e}")

    return db_ok

def storage_status() -> str:
    if not has_db():
        return "/tmp only (add SUPABASE_URL + SUPABASE_KEY for persistence)"
    try:
        r = requests.get(
            _base() + "?key=eq.__ping__&select=key",
            headers=_headers(), timeout=5
        )
        if r.status_code in (200, 406):
            return "Supabase REST API ✅"
        return f"Supabase error: {r.status_code}"
    except Exception as e:
        return f"Supabase unreachable: {str(e)[:60]}"

def migrate_tmp_to_db():
    """Copy /tmp files to Supabase. Safe to run multiple times."""
    if not has_db():
        return "No Supabase credentials configured"
    files = {
        "journal":      "/tmp/flowcheck_journal.json",
        "accounts":     "/tmp/flowcheck_accounts.json",
        "outcomes":     "/tmp/flowcheck_outcomes.json",
        "flow_history": "/tmp/flowcheck_flow_history.json",
    }
    results = []
    for key, path in files.items():
        existing = db_get(key)
        if existing:
            results.append(f"{key}: already in Supabase — skipped")
            continue
        try:
            with open(path) as f:
                data = f.read()
            if db_set(key, data):
                results.append(f"{key}: migrated ✅")
            else:
                results.append(f"{key}: migration failed ❌")
        except FileNotFoundError:
            results.append(f"{key}: no /tmp file")
        except Exception as e:
            results.append(f"{key}: error — {e}")
    return "\n".join(results)
