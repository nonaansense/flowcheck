"""
Persistent storage for FlowCheck using Railway Postgres or Supabase.
Falls back to /tmp files if no database configured.

Set one of these Railway environment variables:
  DATABASE_URL = postgresql://user:pass@host:5432/db  (Railway Postgres)
  SUPABASE_URL + SUPABASE_KEY                          (Supabase)

Without these, data lives in /tmp and resets on redeploy.
"""
import os, json
from datetime import datetime
from zoneinfo import ZoneInfo

def get_db_url() -> str | None:
    """Get database URL from environment."""
    return (
        os.environ.get("DATABASE_URL") or
        os.environ.get("POSTGRES_URL") or
        None
    )

def has_db() -> bool:
    return bool(get_db_url())

# ── Database setup ─────────────────────────────────────────────────────

_db_initialized = False

def init_db():
    """Create tables if they don't exist."""
    global _db_initialized
    if _db_initialized:
        return
    url = get_db_url()
    if not url:
        print("[STORAGE] No database configured — using /tmp files")
        _db_initialized = True
        return
    try:
        import psycopg2
        conn = psycopg2.connect(url, sslmode="require")
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
        print("[STORAGE] Database initialized OK")
    except ImportError:
        print("[STORAGE] psycopg2 not installed — using /tmp files")
        _db_initialized = True
    except Exception as e:
        print(f"[STORAGE] DB init error: {e} — using /tmp files")
        _db_initialized = True

def db_get(key: str) -> str | None:
    """Get a value from database."""
    url = get_db_url()
    if not url:
        return None
    try:
        import psycopg2
        conn = psycopg2.connect(url, sslmode="require")
        cur  = conn.cursor()
        cur.execute("SELECT value FROM flowcheck_store WHERE key = %s", (key,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        return row[0] if row else None
    except Exception as e:
        print(f"[STORAGE] DB get error ({key}): {e}")
        return None

def db_set(key: str, value: str) -> bool:
    """Set a value in database."""
    url = get_db_url()
    if not url:
        return False
    try:
        import psycopg2
        conn = psycopg2.connect(url, sslmode="require")
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
        return True
    except Exception as e:
        print(f"[STORAGE] DB set error ({key}): {e}")
        return False

# ── Unified load/save ──────────────────────────────────────────────────

def load_data(key: str, tmp_path: str, default) -> dict | list:
    """
    Load data from database if available, else from /tmp file.
    key:      database key e.g. 'journal'
    tmp_path: fallback file path e.g. '/tmp/flowcheck_journal.json'
    default:  default value if not found
    """
    init_db()

    # Try database first
    if has_db():
        raw = db_get(key)
        if raw:
            try:
                return json.loads(raw)
            except Exception as e:
                print(f"[STORAGE] JSON parse error ({key}): {e}")

    # Fall back to /tmp file
    try:
        with open(tmp_path) as f:
            data = json.load(f)
        # Migrate to database if available
        if has_db():
            db_set(key, json.dumps(data))
            print(f"[STORAGE] Migrated {key} from /tmp to database")
        return data
    except:
        return default

def save_data(key: str, tmp_path: str, data) -> bool:
    """
    Save data to database if available, AND to /tmp file as backup.
    """
    init_db()
    json_str = json.dumps(data)
    success  = False

    # Save to database
    if has_db():
        success = db_set(key, json_str)
        if success:
            print(f"[STORAGE] Saved {key} to database")

    # Always save to /tmp as backup/fallback
    try:
        with open(tmp_path, "w") as f:
            f.write(json_str)
        if not success:
            success = True
    except Exception as e:
        print(f"[STORAGE] /tmp save error ({tmp_path}): {e}")

    return success

def storage_status() -> str:
    """Return storage status string for health check."""
    if has_db():
        url = get_db_url()
        host = url.split("@")[-1].split("/")[0] if "@" in url else "?"
        return f"PostgreSQL ({host})"
    return "/tmp files (volatile — add DATABASE_URL for persistence)"
