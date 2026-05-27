import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "kit.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS profile (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    user_name TEXT,
    current_visa TEXT,
    gc_stage TEXT,
    priority_date TEXT,
    ac21_eligible TEXT,
    ead TEXT,
    target_roles TEXT,
    master_resume TEXT,
    ollama_url TEXT DEFAULT 'http://localhost:11434',
    ollama_model TEXT DEFAULT 'llama3.1:8b',
    adzuna_app_id TEXT,
    adzuna_app_key TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company TEXT,
    role TEXT,
    jd_url TEXT,
    jd_text TEXT,
    seniority_match TEXT,
    sponsorship_posture TEXT,
    verdict TEXT,
    ac21_used_in_letter INTEGER DEFAULT 0,
    screening_json TEXT,
    resume_md TEXT,
    cover_letter_md TEXT,
    outreach_md TEXT,
    status TEXT DEFAULT 'New',
    notes TEXT,
    date_generated TEXT,
    follow_up_date TEXT,
    created_at TEXT,
    updated_at TEXT
);
"""


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(SCHEMA)
        cur = conn.execute("SELECT COUNT(*) FROM profile WHERE id = 1")
        if cur.fetchone()[0] == 0:
            conn.execute("INSERT INTO profile (id) VALUES (1)")
        conn.commit()


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def fetch_profile() -> dict:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM profile WHERE id = 1").fetchone()
        return dict(row) if row else {}


def update_profile(fields: dict):
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields.keys())
    vals = list(fields.values())
    with get_conn() as conn:
        conn.execute(f"UPDATE profile SET {sets}, updated_at = datetime('now') WHERE id = 1", vals)


def list_applications(status_filter: str | None = None) -> list[dict]:
    q = "SELECT * FROM applications"
    params: list = []
    if status_filter and status_filter != "All":
        q += " WHERE status = ?"
        params.append(status_filter)
    q += " ORDER BY created_at DESC"
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(q, params).fetchall()]


def get_application(app_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone()
        return dict(row) if row else None


def create_application(fields: dict) -> int:
    cols = ", ".join(fields.keys()) + ", created_at, updated_at"
    placeholders = ", ".join("?" for _ in fields) + ", datetime('now'), datetime('now')"
    with get_conn() as conn:
        cur = conn.execute(
            f"INSERT INTO applications ({cols}) VALUES ({placeholders})",
            list(fields.values()),
        )
        return cur.lastrowid


def update_application(app_id: int, fields: dict):
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields.keys())
    vals = list(fields.values()) + [app_id]
    with get_conn() as conn:
        conn.execute(
            f"UPDATE applications SET {sets}, updated_at = datetime('now') WHERE id = ?",
            vals,
        )


def delete_application(app_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM applications WHERE id = ?", (app_id,))


def stats() -> dict:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM applications GROUP BY status"
        ).fetchall()
        by_status = {r["status"]: r["n"] for r in rows}
        total = sum(by_status.values())
        rows = conn.execute(
            "SELECT verdict, COUNT(*) AS n FROM applications GROUP BY verdict"
        ).fetchall()
        by_verdict = {r["verdict"]: r["n"] for r in rows if r["verdict"]}
        return {"total": total, "by_status": by_status, "by_verdict": by_verdict}
