"""Persistent local storage for OpenSky poll batches and aircraft state."""

from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any


class LocalObservationStore:
    """Persist poll batches, current state, and change history on local disk."""

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._raw_dir = data_dir / "raw"
        self._db_path = data_dir / "sqlite" / "opensky_cache.sqlite3"

    def initialize(self) -> None:
        """Create directories and database schema if needed."""
        self._raw_dir.mkdir(parents=True, exist_ok=True)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS poll_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    polled_at INTEGER NOT NULL,
                    status_code INTEGER,
                    success INTEGER NOT NULL,
                    auth_mode TEXT NOT NULL,
                    bbox_json TEXT,
                    bbox_area_sqdeg REAL,
                    latency_ms REAL,
                    rate_limit_remaining INTEGER,
                    retry_after_seconds INTEGER,
                    estimated_credit_cost INTEGER NOT NULL,
                    aircraft_count INTEGER NOT NULL,
                    payload_path TEXT,
                    response_hash TEXT,
                    error_text TEXT
                );

                CREATE TABLE IF NOT EXISTS aircraft_current (
                    icao24 TEXT PRIMARY KEY,
                    first_seen_ts INTEGER NOT NULL,
                    last_seen_ts INTEGER NOT NULL,
                    last_batch_id INTEGER,
                    visibility_status TEXT NOT NULL,
                    unseen_since_ts INTEGER,
                    last_bbox_json TEXT,
                    state_fingerprint TEXT NOT NULL,
                    state_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS aircraft_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    icao24 TEXT NOT NULL,
                    batch_id INTEGER,
                    observed_ts INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    visibility_status TEXT NOT NULL,
                    bbox_json TEXT,
                    state_fingerprint TEXT,
                    state_json TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_batches_polled_at
                    ON poll_batches(polled_at DESC);
                CREATE INDEX IF NOT EXISTS idx_history_icao_observed
                    ON aircraft_history(icao24, observed_ts DESC);
                CREATE INDEX IF NOT EXISTS idx_history_observed_ts
                    ON aircraft_history(observed_ts DESC);
                """
            )

    def restore_snapshot(self, history_minutes: int) -> dict[str, Any]:
        """Restore the latest current state and recent trail history."""
        with self._connect() as conn:
            batch_row = conn.execute(
                """
                SELECT polled_at, bbox_json
                FROM poll_batches
                WHERE success = 1
                ORDER BY polled_at DESC, id DESC
                LIMIT 1
                """
            ).fetchone()
            current_rows = conn.execute(
                """
                SELECT state_json
                FROM aircraft_current
                WHERE visibility_status IN ('seen', 'stale_unseen', 'out_of_scope')
                ORDER BY icao24 ASC
                """
            ).fetchall()
            if batch_row is None:
                return {
                    "fetched_at": None,
                    "active_bbox": None,
                    "states": [],
                    "trails": {},
                }

            cutoff_ts = int(batch_row[0]) - (history_minutes * 60)
            trail_rows = conn.execute(
                """
                SELECT icao24, observed_ts, state_json
                FROM aircraft_history
                WHERE observed_ts >= ?
                  AND event_type IN ('first_seen', 'state_changed', 'seen_again')
                ORDER BY observed_ts ASC, id ASC
                """,
                (cutoff_ts,),
            ).fetchall()

        trails: dict[str, list[dict[str, Any]]] = {}
        for icao24, observed_ts, state_json in trail_rows:
            state = json.loads(state_json)
            point = {
                "t": int(observed_ts),
                "lat": state.get("latitude"),
                "lon": state.get("longitude"),
                "alt": state.get("baro_altitude"),
                "ground": state.get("on_ground"),
            }
            trails.setdefault(str(icao24), []).append(point)

        return {
            "fetched_at": int(batch_row[0]),
            "active_bbox": json.loads(batch_row[1]) if batch_row[1] else None,
            "states": [json.loads(row[0]) for row in current_rows],
            "trails": trails,
        }

    def stats(self) -> dict[str, int]:
        """Return basic storage counts for observability."""
        with self._connect() as conn:
            batch_count = conn.execute("SELECT COUNT(*) FROM poll_batches").fetchone()[
                0
            ]
            current_count = conn.execute(
                "SELECT COUNT(*) FROM aircraft_current"
            ).fetchone()[0]
            history_count = conn.execute(
                "SELECT COUNT(*) FROM aircraft_history"
            ).fetchone()[0]
        return {
            "stored_batch_count": int(batch_count),
            "stored_current_count": int(current_count),
            "stored_history_count": int(history_count),
        }

    def list_poll_batches(
        self,
        *,
        limit: int = 100,
        success_only: bool | None = None,
    ) -> list[dict[str, Any]]:
        """Return recent persisted poll batches with metadata only."""
        query = """
            SELECT
                id,
                polled_at,
                status_code,
                success,
                auth_mode,
                bbox_json,
                bbox_area_sqdeg,
                latency_ms,
                rate_limit_remaining,
                retry_after_seconds,
                estimated_credit_cost,
                aircraft_count,
                payload_path,
                response_hash,
                error_text
            FROM poll_batches
        """
        params: list[Any] = []
        if success_only is not None:
            query += " WHERE success = ?"
            params.append(1 if success_only else 0)
        query += " ORDER BY polled_at DESC, id DESC LIMIT ?"
        params.append(int(limit))

        with self._connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [self._serialize_batch_row(row) for row in rows]

    def get_poll_batch(self, batch_id: int) -> dict[str, Any] | None:
        """Return one persisted poll batch by identifier."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    id,
                    polled_at,
                    status_code,
                    success,
                    auth_mode,
                    bbox_json,
                    bbox_area_sqdeg,
                    latency_ms,
                    rate_limit_remaining,
                    retry_after_seconds,
                    estimated_credit_cost,
                    aircraft_count,
                    payload_path,
                    response_hash,
                    error_text
                FROM poll_batches
                WHERE id = ?
                """,
                (int(batch_id),),
            ).fetchone()
        if row is None:
            return None
        return self._serialize_batch_row(row)

    def list_current_aircraft(
        self,
        *,
        limit: int = 1000,
        visibility_status: str | None = None,
        icao24: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return cached aircraft state with local lifecycle metadata."""
        clauses: list[str] = []
        params: list[Any] = []
        if visibility_status:
            clauses.append("visibility_status = ?")
            params.append(visibility_status)
        if icao24:
            clauses.append("icao24 = ?")
            params.append(icao24.strip().lower())

        query = """
            SELECT
                icao24,
                first_seen_ts,
                last_seen_ts,
                last_batch_id,
                visibility_status,
                unseen_since_ts,
                last_bbox_json,
                state_json
            FROM aircraft_current
        """
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY last_seen_ts DESC, icao24 ASC LIMIT ?"
        params.append(int(limit))

        with self._connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()

        return [
            {
                "icao24": str(row["icao24"]),
                "first_seen_ts": int(row["first_seen_ts"]),
                "last_seen_ts": int(row["last_seen_ts"]),
                "last_batch_id": (
                    int(row["last_batch_id"])
                    if row["last_batch_id"] is not None
                    else None
                ),
                "visibility_status": str(row["visibility_status"]),
                "unseen_since_ts": (
                    int(row["unseen_since_ts"])
                    if row["unseen_since_ts"] is not None
                    else None
                ),
                "last_bbox": (
                    json.loads(row["last_bbox_json"]) if row["last_bbox_json"] else None
                ),
                "state": json.loads(row["state_json"]),
            }
            for row in rows
        ]

    def list_aircraft_history(
        self, *, icao24: str, limit: int = 250
    ) -> list[dict[str, Any]]:
        """Return persisted visibility and state-change history for one aircraft."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    id,
                    batch_id,
                    observed_ts,
                    event_type,
                    visibility_status,
                    bbox_json,
                    state_fingerprint,
                    state_json
                FROM aircraft_history
                WHERE icao24 = ?
                ORDER BY observed_ts DESC, id DESC
                LIMIT ?
                """,
                (icao24.strip().lower(), int(limit)),
            ).fetchall()

        return [
            {
                "id": int(row["id"]),
                "icao24": icao24.strip().lower(),
                "batch_id": int(row["batch_id"])
                if row["batch_id"] is not None
                else None,
                "observed_ts": int(row["observed_ts"]),
                "event_type": str(row["event_type"]),
                "visibility_status": str(row["visibility_status"]),
                "bbox": json.loads(row["bbox_json"]) if row["bbox_json"] else None,
                "state_fingerprint": row["state_fingerprint"],
                "state": json.loads(row["state_json"]) if row["state_json"] else None,
            }
            for row in rows
        ]

    def get_batch_payload(self, batch_id: int) -> dict[str, Any] | None:
        """Return a raw archived OpenSky payload for one poll batch."""
        batch = self.get_poll_batch(batch_id)
        if batch is None or not batch["payload_path"]:
            return None

        payload_path = self._resolve_payload_path(str(batch["payload_path"]))
        if not payload_path.exists():
            raise FileNotFoundError(f"cached payload not found for batch {batch_id}")

        with gzip.open(payload_path, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
        return {"batch": batch, "payload": payload}

    def usage_summary(self) -> dict[str, int | None]:
        """Return persisted usage totals and the start of the usage window."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COALESCE(SUM(estimated_credit_cost), 0),
                    MIN(polled_at),
                    COUNT(*),
                    COALESCE(SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END), 0)
                FROM poll_batches
                """
            ).fetchone()
        return {
            "estimated_credit_usage": int(row[0]),
            "usage_window_started_at": int(row[1]) if row[1] is not None else None,
            "fetch_count": int(row[2]),
            "successful_fetch_count": int(row[3]),
        }

    def record_poll(
        self,
        *,
        polled_at: int,
        status_code: int | None,
        success: bool,
        auth_mode: str,
        bbox: dict[str, Any] | None,
        bbox_area_sqdeg: float | None,
        latency_ms: float | None,
        rate_limit_remaining: int | None,
        retry_after_seconds: int | None,
        estimated_credit_cost: int,
        aircraft_states: list[dict[str, Any]],
        raw_payload: dict[str, Any] | None,
        error_text: str | None,
    ) -> dict[str, int]:
        """Persist one poll batch and update current/history state."""
        bbox_json = self._stable_json(bbox) if bbox else None
        payload_path: str | None = None
        response_hash: str | None = None
        if raw_payload is not None:
            payload_path, response_hash = self._write_payload(
                polled_at=polled_at,
                status_code=status_code,
                payload=raw_payload,
            )

        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO poll_batches (
                    polled_at,
                    status_code,
                    success,
                    auth_mode,
                    bbox_json,
                    bbox_area_sqdeg,
                    latency_ms,
                    rate_limit_remaining,
                    retry_after_seconds,
                    estimated_credit_cost,
                    aircraft_count,
                    payload_path,
                    response_hash,
                    error_text
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    polled_at,
                    status_code,
                    1 if success else 0,
                    auth_mode,
                    bbox_json,
                    bbox_area_sqdeg,
                    latency_ms,
                    rate_limit_remaining,
                    retry_after_seconds,
                    estimated_credit_cost,
                    len(aircraft_states),
                    payload_path,
                    response_hash,
                    error_text,
                ),
            )
            batch_id = int(cursor.lastrowid)
            if success:
                self._record_successful_states(
                    conn,
                    batch_id=batch_id,
                    polled_at=polled_at,
                    bbox_json=bbox_json,
                    aircraft_states=aircraft_states,
                )

        return self.stats()

    def _record_successful_states(
        self,
        conn: sqlite3.Connection,
        *,
        batch_id: int,
        polled_at: int,
        bbox_json: str | None,
        aircraft_states: list[dict[str, Any]],
    ) -> None:
        observed_icao24: set[str] = set()

        for state in aircraft_states:
            icao24 = str(state.get("icao24") or "").strip()
            if not icao24:
                continue
            observed_icao24.add(icao24)
            state_json = self._stable_json(state)
            fingerprint = self._hash_text(state_json)
            row = conn.execute(
                """
                SELECT first_seen_ts, visibility_status, state_fingerprint
                FROM aircraft_current
                WHERE icao24 = ?
                """,
                (icao24,),
            ).fetchone()

            if row is None:
                first_seen_ts = polled_at
                event_type = "first_seen"
            else:
                first_seen_ts = int(row[0])
                previous_status = str(row[1])
                previous_fingerprint = str(row[2])
                if previous_fingerprint != fingerprint:
                    event_type = "state_changed"
                elif previous_status != "seen":
                    event_type = "seen_again"
                else:
                    event_type = "no_change"

            conn.execute(
                """
                INSERT INTO aircraft_current (
                    icao24,
                    first_seen_ts,
                    last_seen_ts,
                    last_batch_id,
                    visibility_status,
                    unseen_since_ts,
                    last_bbox_json,
                    state_fingerprint,
                    state_json
                ) VALUES (?, ?, ?, ?, 'seen', NULL, ?, ?, ?)
                ON CONFLICT(icao24) DO UPDATE SET
                    last_seen_ts = excluded.last_seen_ts,
                    last_batch_id = excluded.last_batch_id,
                    visibility_status = excluded.visibility_status,
                    unseen_since_ts = excluded.unseen_since_ts,
                    last_bbox_json = excluded.last_bbox_json,
                    state_fingerprint = excluded.state_fingerprint,
                    state_json = excluded.state_json
                """,
                (
                    icao24,
                    first_seen_ts,
                    polled_at,
                    batch_id,
                    bbox_json,
                    fingerprint,
                    state_json,
                ),
            )

            if event_type != "no_change":
                conn.execute(
                    """
                    INSERT INTO aircraft_history (
                        icao24,
                        batch_id,
                        observed_ts,
                        event_type,
                        visibility_status,
                        bbox_json,
                        state_fingerprint,
                        state_json
                    ) VALUES (?, ?, ?, ?, 'seen', ?, ?, ?)
                    """,
                    (
                        icao24,
                        batch_id,
                        polled_at,
                        event_type,
                        bbox_json,
                        fingerprint,
                        state_json,
                    ),
                )

        if observed_icao24:
            unseen_rows = conn.execute(
                """
                SELECT icao24, visibility_status, last_bbox_json, state_fingerprint, state_json
                FROM aircraft_current
                WHERE icao24 NOT IN ({placeholders})
                """.format(placeholders=",".join("?" for _ in observed_icao24)),
                tuple(sorted(observed_icao24)),
            ).fetchall()
        else:
            unseen_rows = conn.execute(
                """
                SELECT icao24, visibility_status, last_bbox_json, state_fingerprint, state_json
                FROM aircraft_current
                """
            ).fetchall()

        for (
            icao24,
            visibility_status,
            last_bbox_json,
            fingerprint,
            state_json,
        ) in unseen_rows:
            next_status = (
                "stale_unseen" if last_bbox_json == bbox_json else "out_of_scope"
            )
            if visibility_status == next_status:
                continue
            conn.execute(
                """
                UPDATE aircraft_current
                SET visibility_status = ?,
                    unseen_since_ts = ?,
                    last_batch_id = ?
                WHERE icao24 = ?
                """,
                (
                    next_status,
                    polled_at if next_status == "stale_unseen" else None,
                    batch_id,
                    icao24,
                ),
            )
            conn.execute(
                """
                INSERT INTO aircraft_history (
                    icao24,
                    batch_id,
                    observed_ts,
                    event_type,
                    visibility_status,
                    bbox_json,
                    state_fingerprint,
                    state_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    icao24,
                    batch_id,
                    polled_at,
                    next_status,
                    next_status,
                    bbox_json,
                    fingerprint,
                    state_json,
                ),
            )

    def _write_payload(
        self,
        *,
        polled_at: int,
        status_code: int | None,
        payload: dict[str, Any],
    ) -> tuple[str, str]:
        """Write a compressed JSON payload to disk and return path plus hash."""
        payload_json = self._stable_json(payload)
        response_hash = self._hash_text(payload_json)
        timestamp_text = str(polled_at)
        directory = (
            self._raw_dir / timestamp_text[:4] / timestamp_text[4:6]
            if len(timestamp_text) >= 6
            else self._raw_dir
        )
        directory.mkdir(parents=True, exist_ok=True)
        file_name = f"poll_{polled_at}_{status_code or 0}_{response_hash[:12]}.json.gz"
        path = directory / file_name
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            handle.write(payload_json)
        return str(path), response_hash

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _serialize_batch_row(self, row: sqlite3.Row) -> dict[str, Any]:
        payload_path = (
            self._relative_payload_path(str(row["payload_path"]))
            if row["payload_path"]
            else None
        )
        return {
            "id": int(row["id"]),
            "polled_at": int(row["polled_at"]),
            "status_code": (
                int(row["status_code"]) if row["status_code"] is not None else None
            ),
            "success": bool(row["success"]),
            "auth_mode": str(row["auth_mode"]),
            "bbox": json.loads(row["bbox_json"]) if row["bbox_json"] else None,
            "bbox_area_sqdeg": (
                float(row["bbox_area_sqdeg"])
                if row["bbox_area_sqdeg"] is not None
                else None
            ),
            "latency_ms": (
                float(row["latency_ms"]) if row["latency_ms"] is not None else None
            ),
            "rate_limit_remaining": (
                int(row["rate_limit_remaining"])
                if row["rate_limit_remaining"] is not None
                else None
            ),
            "retry_after_seconds": (
                int(row["retry_after_seconds"])
                if row["retry_after_seconds"] is not None
                else None
            ),
            "estimated_credit_cost": int(row["estimated_credit_cost"]),
            "aircraft_count": int(row["aircraft_count"]),
            "payload_path": payload_path,
            "payload_available": payload_path is not None,
            "response_hash": row["response_hash"],
            "error_text": row["error_text"],
        }

    def _relative_payload_path(self, payload_path: str) -> str:
        path = Path(payload_path)
        try:
            return str(path.resolve().relative_to(self._data_dir.resolve()))
        except ValueError:
            return path.name

    def _resolve_payload_path(self, payload_path: str) -> Path:
        path = Path(payload_path)
        if not path.is_absolute():
            path = self._data_dir / path
        resolved = path.resolve()
        resolved.relative_to(self._data_dir.resolve())
        return resolved

    def _stable_json(self, payload: Any) -> str:
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    def _hash_text(self, payload: str) -> str:
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
