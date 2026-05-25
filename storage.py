"""
Persistent storage for FlowCheck using PostgreSQL (Supabase or Railway Postgres).
Falls back to /tmp files if no database configured.

Set DATABASE_URL in Railway environment variables:
  postgresql://postgres:[password]@db.[ref].supabase.co:5432/postgres
"""
import os, json
from datetime import datetime

def get_db_url() -> str | None:
    return (
        os.environ.get("DATABASE_URL") or
        os.environ.get("POSTGRES_URL") or
        None
    )

def has_db() -> bool:
    return bool(get_db_url())

_db_initialized = False

def get_connection():
    """Get a fresh database connection."""
    import psycopg2
    url = get_db_url()
    if not url:
        return None
    # Supabase requires SSL — handle both url formats
    if "sslmode" not in url:
        return psycopg2.connect(url, sslmode="require")
    return psycopg2.connect(url)

def init_db():
    """Create table if it doesn't exist. Called once on startup."""
    global _db_initialized
    if _db_initialized:
        return
    if not has_db():
        print("[STORAGE] No DATABASE_URL — using /tmp (data lost on redeploy)")
        _db_initialized = True
        return
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS flowcheck_store (
                key     TEXT PRIMARY KEY,
                value   TEXT NOT NULL,
                updated TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        conn.commit()
        cur.close()
        conn.close()
        _db_initialized = True
        print("[STORAGE] Supabase connected and table ready")
    except ImportError:
        print("[STORAGE] psycopg2 not installed — using /tmp")
        _db_initialized = True
    except Exception as e:
        print(f"[STORAGE] DB init failed: {e} — using /tmp")
        _db_initialized = True

def db_get(key: str) -> str | None:
    """Fetch value from database. Returns None on any error."""
    if not has_db():
        return None
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("SELECT value FROM flowcheck_store WHERE key = %s", (key,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row:
            print(f"[STORAGE] Loaded '{key}' from Supabase ({len(row[0])} chars)")
            return row[0]
        print(f"[STORAGE] Key '{key}' not found in Supabase")
        return None
    except Exception as e:
        print(f"[STORAGE] db_get error ({key}): {e}")
        return None

def db_set(key: str, value: str) -> bool:
    """Save value to database. Returns True on success."""
    if not has_db():
        return False
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("""
            INSERT INTO flowcheck_store (key, value, updated)
            VALUES (%s, %s, NOW())
            ON CONFLICT (key) DO UPDATE
            SET value = EXCLUDED.value, updated = NOW()
        """, (key, value))
        conn.commit()
        cur.close()
        conn.close()
        print(f"[STORAGE] Saved '{key}' to Supabase ({len(value)} chars)")
        return True
    except Exception as e:
        print(f"[STORAGE] db_set error ({key}): {e}")
        return False

def load_data(key: str, tmp_path: str, default) -> dict | list:
    """
    Load from Supabase if available, else /tmp file, else default.
    NEVER migrates /tmp → Supabase to avoid overwriting real data.
    """
    init_db()

    # 1. Try Supabase first (authoritative source)
    if has_db():
        raw = db_get(key)
        if raw:
            try:
                data = json.loads(raw)
                return data
            except Exception as e:
                print(f"[STORAGE] JSON parse error ({key}): {e}")

    # 2. Fall back to /tmp (only if Supabase unavailable or key not found)
    try:
        with open(tmp_path) as f:
            data = json.load(f)
        print(f"[STORAGE] Loaded '{key}' from /tmp (Supabase miss)")
        return data
    except:
        pass

    # 3. Return default
    print(f"[STORAGE] No data found for '{key}' — returning default")
    return default

def save_data(key: str, tmp_path: str, data) -> bool:
    """
    Save to Supabase (primary) and /tmp (backup).
    Supabase is always written first.
    """
    init_db()
    json_str = json.dumps(data)
    success  = False

    # 1. Save to Supabase
    if has_db():
        success = db_set(key, json_str)

    # 2. Always save to /tmp as local backup
    try:
        with open(tmp_path, "w") as f:
            f.write(json_str)
        if not success:
            success = True
            print(f"[STORAGE] Saved '{key}' to /tmp only (Supabase unavailable)")
    except Exception as e:
        print(f"[STORAGE] /tmp save error ({tmp_path}): {e}")

    return success

def storage_status() -> str:
    """Human readable storage status."""
    if not has_db():
        return "/tmp files (volatile — add DATABASE_URL for persistence)"
    url  = get_db_url()
    host = url.split("@")[-1].split("/")[0] if "@" in url else "unknown"
    # Test connection
    try:
        conn = get_connection()
        conn.close()
        return f"Supabase PostgreSQL ({host}) ✅"
    except Exception as e:
        return f"Supabase configured but connection failed: {str(e)[:60]}"

def migrate_tmp_to_db():
    """
    One-time migration: copy existing /tmp files to Supabase.
    Only runs if Supabase key is empty (first time setup).
    Call this manually via /migrate endpoint if needed.
    """
    if not has_db():
        return "No database configured"

    files = {
        "journal":      "/tmp/flowcheck_journal.json",
        "outcomes":     "/tmp/flowcheck_outcomes.json",
        "flow_history": "/tmp/flowcheck_flow_history.json",
        "sector_flows": "/tmp/flowcheck_sector_flows.json",
    }

    results = []
    for key, path in files.items():
        # Only migrate if Supabase doesn't already have this key
        existing = db_get(key)
        if existing:
            results.append(f"{key}: already in Supabase — skipped")
            continue
        try:
            with open(path) as f:
                data = f.read()
            if db_set(key, data):
                results.append(f"{key}: migrated from /tmp ✅")
            else:
                results.append(f"{key}: migration failed ❌")
        except FileNotFoundError:
            results.append(f"{key}: no /tmp file — skipped")
        except Exception as e:
            results.append(f"{key}: error — {e}")

    return "\n".join(results)
