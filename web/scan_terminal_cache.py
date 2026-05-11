from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# Shared SQLite connection (all workers write to the same file)
# ---------------------------------------------------------------------------
_DB_PATH = Path(__file__).resolve().parents[1] / "data" / "polyweather.db"
_DB_LOCK = threading.Lock()


def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_DB_PATH), timeout=30.0, isolation_level="IMMEDIATE")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


# ---------------------------------------------------------------------------
# In-memory cache for AI results (not shared across workers, fine for now)
# ---------------------------------------------------------------------------
_SCAN_TERMINAL_AI_CACHE_LOCK = threading.Lock()
_SCAN_TERMINAL_AI_CACHE: Dict[str, Dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# Key helpers
# ---------------------------------------------------------------------------

def scan_terminal_cache_key(filters: Dict[str, Any]) -> str:
    return json.dumps(filters, ensure_ascii=True, sort_keys=True)


# ---------------------------------------------------------------------------
# Shared scan-terminal cache (SQLite-backed)
# ---------------------------------------------------------------------------

def get_cached_scan_terminal_payload(
    filters: Dict[str, Any],
    *,
    ttl_sec: int,
) -> Optional[Dict[str, Any]]:
    cache_key = scan_terminal_cache_key(filters)
    now = time.time()
    with _DB_LOCK:
        conn = _get_db()
        row = conn.execute(
            "SELECT payload_json, cached_at_ts FROM scan_terminal_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
        if not row:
            return None
        payload_json_str, cached_at = row
        if now - float(cached_at) >= float(ttl_sec):
            return None
        try:
            return json.loads(payload_json_str)
        except (json.JSONDecodeError, TypeError):
            return None


def get_scan_terminal_cache_entry(filters: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    cache_key = scan_terminal_cache_key(filters)
    with _DB_LOCK:
        conn = _get_db()
        row = conn.execute(
            """SELECT payload_json, cached_at_ts, success_at_ts,
                      success_payload, last_error, last_failed_at
               FROM scan_terminal_cache WHERE cache_key = ?""",
            (cache_key,),
        ).fetchone()
        if not row:
            return None
        return {
            "payload_json": row[0],
            "t": row[1],
            "success_t": row[2],
            "success_payload": row[3],
            "last_error": row[4],
            "last_failed_at": row[5],
        }


def set_cached_scan_terminal_payload(
    filters: Dict[str, Any],
    payload: Dict[str, Any],
) -> None:
    cache_key = scan_terminal_cache_key(filters)
    existing = get_scan_terminal_cache_entry(filters) or {}
    now = time.time()
    payload_json = json.dumps(payload, ensure_ascii=True)
    success_payload_json = json.dumps(payload, ensure_ascii=True)
    with _DB_LOCK:
        conn = _get_db()
        conn.execute(
            """INSERT INTO scan_terminal_cache
               (cache_key, payload_json, cached_at_ts, success_at_ts,
                success_payload, last_error, last_failed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(cache_key) DO UPDATE SET
                   payload_json   = excluded.payload_json,
                   cached_at_ts   = excluded.cached_at_ts,
                   success_at_ts  = excluded.success_at_ts,
                   success_payload= excluded.success_payload,
                   last_error     = excluded.last_error,
                   last_failed_at = excluded.last_failed_at""",
            (
                cache_key,
                payload_json,
                now,
                now,
                success_payload_json,
                existing.get("last_error"),
                existing.get("last_failed_at"),
            ),
        )
        conn.commit()


def set_scan_terminal_failure_state(
    filters: Dict[str, Any],
    *,
    error_message: str,
) -> None:
    cache_key = scan_terminal_cache_key(filters)
    now = time.time()
    now_iso = datetime.utcnow().isoformat() + "Z"
    with _DB_LOCK:
        conn = _get_db()
        # Only update the error fields, preserve everything else
        existing = conn.execute(
            "SELECT payload_json, cached_at_ts, success_at_ts, success_payload, last_failed_at FROM scan_terminal_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
        if existing:
            conn.execute(
                """UPDATE scan_terminal_cache SET
                       last_error = ?, last_failed_at = ?
                   WHERE cache_key = ?""",
                (error_message, now_iso, cache_key),
            )
        else:
            conn.execute(
                """INSERT INTO scan_terminal_cache
                   (cache_key, payload_json, cached_at_ts, last_error, last_failed_at)
                   VALUES (?, '{}', ?, ?, ?)""".replace("'{}'", " '{}' "),
                (cache_key, "{}", now, error_message, now_iso),
            )
        conn.commit()


# ---------------------------------------------------------------------------
# Cross-worker refresh lock using the shared cache_refresh_locks table
# ---------------------------------------------------------------------------

def mark_scan_terminal_refreshing(filters: Dict[str, Any]) -> bool:
    """Atomically try to claim the refresh lock. Returns True if we claimed it."""
    cache_key = scan_terminal_cache_key(filters)
    now = time.time()
    lock_ttl = 300.0  # auto-expire after 5 minutes
    with _DB_LOCK:
        conn = _get_db()
        # Try to insert a new lock (fails if one exists and not expired)
        try:
            conn.execute(
                """INSERT INTO cache_refresh_locks
                   (cache_key, locked_until_ts, owner, updated_at)
                   VALUES (?, ?, ?, ?)""",
                (cache_key, now + lock_ttl, "scan-terminal", datetime.utcnow().isoformat() + "Z"),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            # Lock exists — check if expired
            row = conn.execute(
                "SELECT locked_until_ts FROM cache_refresh_locks WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
            if row and now > float(row[0]):
                # Expired — update it
                conn.execute(
                    """UPDATE cache_refresh_locks SET
                           locked_until_ts = ?, owner = ?, updated_at = ?
                       WHERE cache_key = ?""",
                    (now + lock_ttl, "scan-terminal", datetime.utcnow().isoformat() + "Z", cache_key),
                )
                conn.commit()
                return True
            return False


def clear_scan_terminal_refreshing(filters: Dict[str, Any]) -> None:
    cache_key = scan_terminal_cache_key(filters)
    with _DB_LOCK:
        conn = _get_db()
        conn.execute("DELETE FROM cache_refresh_locks WHERE cache_key = ?", (cache_key,))
        conn.commit()


# ---------------------------------------------------------------------------
# AI cache (remains in-process, not shared — acceptable for per-request AI results)
# ---------------------------------------------------------------------------

def scan_ai_cache_key(
    snapshot_id: str,
    filters: Dict[str, Any],
    *,
    max_rows: int,
    model: str,
) -> str:
    raw = json.dumps(
        {
            "schema_version": "city_forecast_v1",
            "snapshot_id": snapshot_id,
            "filters": filters,
            "model": model,
            "max_rows": max_rows,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get_cached_scan_ai_result(
    snapshot_id: str,
    filters: Dict[str, Any],
    *,
    max_rows: int,
    model: str,
    ttl_sec: int,
) -> Optional[Dict[str, Any]]:
    cache_key = scan_ai_cache_key(
        snapshot_id,
        filters,
        max_rows=max_rows,
        model=model,
    )
    now = time.time()
    with _SCAN_TERMINAL_AI_CACHE_LOCK:
        cached = _SCAN_TERMINAL_AI_CACHE.get(cache_key)
        if not cached:
            return None
        cached_at = float(cached.get("cached_at") or 0.0)
        if now - cached_at >= float(ttl_sec):
            return None
        result = cached.get("result")
        if isinstance(result, dict):
            return dict(result)
    return None


def set_cached_scan_ai_result(
    snapshot_id: str,
    filters: Dict[str, Any],
    result: Dict[str, Any],
    *,
    max_rows: int,
    model: str,
) -> None:
    cache_key = scan_ai_cache_key(
        snapshot_id,
        filters,
        max_rows=max_rows,
        model=model,
    )
    with _SCAN_TERMINAL_AI_CACHE_LOCK:
        _SCAN_TERMINAL_AI_CACHE[cache_key] = {
            "cached_at": time.time(),
            "result": result,
        }
