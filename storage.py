"""
Persistent storage for FlowCheck using Supabase PostgreSQL.
Every save logs explicitly to Railway console so you can see what's happening.

Set DATABASE_URL in Railway environment variables.
"""
import os, json

def get_db_url() -> str | None:
    return (
        os.environ.get("DATABASE_URL") or
        os.environ.get("POSTGRES_URL") or
        None
    )

def has_db() -> bool:
    return bool(get_db_url())

_db_initialized = False

def get_conn():
    """Open a fresh Supabase connection with SSL."""
    try:
        import psycopg2
        url = get_db_url()
        if not url:
            return None
        # Force SSL for Supabase
        if "sslmode" not in url:
            conn = psycopg2.connect(url, sslmode="require", connect_timeout=10)
        else:
            conn = psycopg2.connect(url, connect_timeout=10)
        return conn
    except Exception as e:
        print(f"[STORAGE] Connection failed: {e}")
        return None

def init_db():
    global _db_initialized
    if _db_initialized:
        return
    if not has_db():
        print("[STORAGE] ⚠️  No DATABASE_URL — data will be lost on redeploy!")
        _db_initialized = True
        return
    conn = get_conn()
    if not conn:
        print("[STORAGE] ⚠️  Cannot connect to Supabase — data will be lost on redeploy!")
        _db_initialized = True
        return
    try:
        cur = conn.cursor()
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
        print("[STORAGE] ✅ Supabase connected — data will persist across redeploys")
    except Exception as e:
        print(f"[STORAGE] ⚠️  Init error: {e}")
        _db_initialized = True

def db_get(key: str) -> str | None:
    """Read from Supabase. Returns None if unavailable."""
    if not has_db():
        return None
    conn = get_conn()
    if not conn:
        print(f"[STORAGE] ⚠️  db_get({key}): no connection")
        return None
    try:
        cur = conn.cursor()
        cur.execute("SELECT value FROM flowcheck_store WHERE key = %s", (key,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row:
            print(f"[STORAGE] ✅ Loaded '{key}' from Supabase ({len(row[0])} chars)")
            return row[0]
        print(f"[STORAGE] '{key}' not in Supabase yet")
        return None
    except Exception as e:
        print(f"[STORAGE] ⚠️  db_get({key}) error: {e}")
        try: conn.close()
        except: pass
        return None

def db_set(key: str, value: str) -> bool:
    """Write to Supabase. Returns True on success."""
    if not has_db():
        print(f"[STORAGE] ⚠️  db_set({key}): no DATABASE_URL")
        return False
    conn = get_conn()
    if not conn:
        print(f"[STORAGE] ⚠️  db_set({key}): no connection — NOT saved to Supabase!")
        return False
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO flowcheck_store (key, value, updated)
            VALUES (%s, %s, NOW())
            ON CONFLICT (key) DO UPDATE
            SET value = EXCLUDED.value, updated = NOW()
        """, (key, value))
        conn.commit()
        cur.close()
        conn.close()
        print(f"[STORAGE] ✅ Saved '{key}' to Supabase ({len(value)} chars)")
        return True
    except Exception as e:
        print(f"[STORAGE] ⚠️  db_set({key}) FAILED: {e} — NOT saved to Supabase!")
        try: conn.close()
        except: pass
        return False

def load_data(key: str, tmp_path: str, default) -> dict | list:
    """Load from Supabase first, then /tmp, then default."""
    init_db()

    # 1. Supabase (authoritative)
    if has_db():
        raw = db_get(key)
        if raw:
            try:
                return json.loads(raw)
            except Exception as e:
                print(f"[STORAGE] JSON parse error ({key}): {e}")

    # 2. /tmp fallback
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
    """Save to Supabase AND /tmp."""
    init_db()
    json_str = json.dumps(data)
    db_ok    = False

    # 1. Supabase first
    if has_db():
        db_ok = db_set(key, json_str)
        if not db_ok:
            print(f"[STORAGE] ⚠️  '{key}' NOT in Supabase — will be lost on redeploy!")

    # 2. Always /tmp as local cache
    try:
        with open(tmp_path, "w") as f:
            f.write(json_str)
    except Exception as e:
        print(f"[STORAGE] /tmp save error ({tmp_path}): {e}")

    return db_ok

def storage_status() -> str:
    if not has_db():
        return "/tmp only — add DATABASE_URL for persistence"
    conn = get_conn()
    if conn:
        conn.close()
        return "Supabase ✅"
    return "Supabase configured but connection failing ⚠️"

def migrate_tmp_to_db():
    """Copy /tmp files to Supabase. Skips keys already in Supabase."""
    if not has_db():
        return "No database configured"
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
