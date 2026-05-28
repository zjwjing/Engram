"""
Engram → XERIS Relay — HTTP Client

Submits signed relay payloads to the XERIS endpoint.

Configuration (environment variables):
    XERIS_RELAY_URL      — XERIS submission endpoint (required to actually relay)
    XERIS_RELAY_TIMEOUT  — request timeout in seconds (default 10)
    ENGRAM_RELAY_ENABLED — set to "false" to disable relay without code changes

Dead-letter queue (DLQ):
    Failed submissions are persisted to SQLite and retried automatically on the
    next emit() call with exponential backoff (10s, 20s, 40s … capped at 300s).
    After 10 failed attempts the entry is marked "abandoned" and logged as an
    error — it is never silently dropped.

Dry-run mode:
    When XERIS_RELAY_URL is unset the client operates in dry-run mode: payloads
    are built, signed, and logged but not submitted. Safe to deploy before XERIS
    is ready.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from engram.relay.adapter import RelayPayload, build_payload
from engram.relay.signer import sign_payload

logger = logging.getLogger(__name__)

_DEFAULT_LOG_DB = Path(os.getenv("RELAY_LOG_DB", "data/relay_log.db"))
_MAX_ATTEMPTS   = 10
_BACKOFF_BASE   = 10    # seconds
_BACKOFF_CAP    = 300   # seconds


def _backoff(attempts: int) -> float:
    return min(_BACKOFF_CAP, _BACKOFF_BASE * (2 ** attempts))


class RelayClient:
    """
    Submits scored validator round results to XERIS as signed payloads.

    On failure, payloads enter a dead-letter queue and are retried automatically
    with exponential backoff. Nothing is ever silently dropped.

    Args:
        xeris_url:  XERIS relay endpoint. If None or empty, dry-run mode.
        keypair:    ``bt.Keypair`` for signing. If None, payloads are unsigned
                    (only valid in dry-run mode).
        timeout:    HTTP request timeout in seconds.
        log_db:     Path to SQLite submission log and DLQ.
        enabled:    Set False to disable relay entirely.
    """

    def __init__(
        self,
        xeris_url: str | None = None,
        keypair: Any = None,
        timeout: float = 10.0,
        log_db: Path = _DEFAULT_LOG_DB,
        enabled: bool = True,
    ) -> None:
        self._url     = xeris_url or ""
        self._keypair = keypair
        self._timeout = timeout
        self._log_db  = log_db
        self._enabled = enabled and bool(self._url)
        if self._url and not enabled:
            logger.info("Relay disabled via ENGRAM_RELAY_ENABLED=false")
        elif not self._url:
            logger.info("Relay in dry-run mode — XERIS_RELAY_URL not set")
        self._db = self._open_db()

    @classmethod
    def from_env(cls, keypair: Any = None) -> "RelayClient":
        """Construct from environment variables."""
        return cls(
            xeris_url=os.getenv("XERIS_RELAY_URL", ""),
            keypair=keypair,
            timeout=float(os.getenv("XERIS_RELAY_TIMEOUT", "10")),
            enabled=os.getenv("ENGRAM_RELAY_ENABLED", "true").lower() != "false",
        )

    # ── Public API ─────────────────────────────────────────────────────────────

    def emit(
        self,
        recall_scores: dict[int, float],
        latency_scores: dict[int, float | None],
        proof_rates: dict[int, float],
        *,
        netuid: int = 450,
        block: int = 0,
        validator_uid: int = 0,
    ) -> bool:
        """
        Build, sign, and submit a relay payload for one scoring round.

        Retries any pending DLQ entries first, then submits the new payload.
        On failure the new payload is enqueued in the DLQ for future retries.

        Returns True if the new payload was submitted successfully (or dry-run).
        """
        if self._enabled:
            self._flush_dlq()

        payload = build_payload(
            recall_scores, latency_scores, proof_rates,
            netuid=netuid, block=block, validator_uid=validator_uid,
        )
        return self._submit(payload)

    def status(self) -> dict[str, Any]:
        """
        Return a health summary for monitoring.

        Keys:
            enabled        — whether live submission is active
            dlq_pending    — entries awaiting retry
            dlq_abandoned  — entries that exhausted all retries
            total_ok       — lifetime successful submissions
            total_failed   — lifetime failures (including DLQ entries)
            last_ok_hash   — output_hash of the most recent successful submission
            last_ok_at     — unix timestamp of the most recent success
        """
        row = self._db.execute(
            "SELECT COUNT(*) FROM relay_dlq WHERE status='pending'"
        ).fetchone()
        dlq_pending = row[0] if row else 0

        row = self._db.execute(
            "SELECT COUNT(*) FROM relay_dlq WHERE status='abandoned'"
        ).fetchone()
        dlq_abandoned = row[0] if row else 0

        row = self._db.execute(
            "SELECT COUNT(*) FROM relay_log WHERE status='ok'"
        ).fetchone()
        total_ok = row[0] if row else 0

        row = self._db.execute(
            "SELECT COUNT(*) FROM relay_log WHERE status NOT IN ('ok','dry_run')"
        ).fetchone()
        total_failed = row[0] if row else 0

        row = self._db.execute(
            "SELECT output_hash, submitted_at FROM relay_log "
            "WHERE status='ok' ORDER BY submitted_at DESC LIMIT 1"
        ).fetchone()
        last_ok_hash = row[0] if row else None
        last_ok_at   = row[1] if row else None

        return {
            "enabled":       self._enabled,
            "dlq_pending":   dlq_pending,
            "dlq_abandoned": dlq_abandoned,
            "total_ok":      total_ok,
            "total_failed":  total_failed,
            "last_ok_hash":  last_ok_hash,
            "last_ok_at":    last_ok_at,
        }

    # ── DLQ ────────────────────────────────────────────────────────────────────

    def _flush_dlq(self) -> None:
        """Retry all DLQ entries whose next_retry_at has passed."""
        now = time.time()
        rows = self._db.execute(
            "SELECT id, output_hash, block, signed_json, attempts "
            "FROM relay_dlq WHERE status='pending' AND next_retry_at <= ?",
            (now,),
        ).fetchall()

        for row_id, output_hash, block, signed_json, attempts in rows:
            ok = self._http_post(signed_json.encode("utf-8"), output_hash, block)
            if ok:
                self._db.execute(
                    "UPDATE relay_dlq SET status='delivered', attempts=? WHERE id=?",
                    (attempts + 1, row_id),
                )
                logger.info("DLQ delivered | output_hash=%s | after %d attempt(s)", output_hash[:16], attempts + 1)
            else:
                new_attempts = attempts + 1
                if new_attempts >= _MAX_ATTEMPTS:
                    self._db.execute(
                        "UPDATE relay_dlq SET status='abandoned', attempts=? WHERE id=?",
                        (new_attempts, row_id),
                    )
                    logger.error(
                        "DLQ abandoned | output_hash=%s | exhausted %d attempts",
                        output_hash[:16], new_attempts,
                    )
                else:
                    retry_at = now + _backoff(new_attempts)
                    self._db.execute(
                        "UPDATE relay_dlq SET attempts=?, next_retry_at=? WHERE id=?",
                        (new_attempts, retry_at, row_id),
                    )
                    logger.warning(
                        "DLQ retry scheduled | output_hash=%s | attempt=%d | retry_in=%.0fs",
                        output_hash[:16], new_attempts, retry_at - now,
                    )
        self._db.commit()

    def _enqueue_dlq(self, signed_json: str, output_hash: str, block: int) -> None:
        retry_at = time.time() + _backoff(0)
        self._db.execute(
            "INSERT INTO relay_dlq (output_hash, block, signed_json, attempts, next_retry_at, status) "
            "VALUES (?,?,?,0,?,'pending')",
            (output_hash, block, signed_json, retry_at),
        )
        self._db.commit()
        logger.warning("Relay failed — enqueued in DLQ | output_hash=%s | retry_in=%.0fs",
                       output_hash[:16], _backoff(0))

    # ── HTTP ───────────────────────────────────────────────────────────────────

    def _submit(self, payload: RelayPayload) -> bool:
        signed: dict[str, Any]
        if self._keypair is not None:
            signed = sign_payload(payload, self._keypair)
        else:
            signed = payload.to_dict()

        body = json.dumps(signed, separators=(",", ":"))

        if not self._enabled:
            logger.debug("Relay dry-run | output_hash=%s | miners=%d",
                         payload.output_hash[:16], len(payload.scores))
            self._log(payload.output_hash, payload.block, "dry_run", None)
            return True

        ok = self._http_post(body.encode("utf-8"), payload.output_hash, payload.block)
        if not ok:
            self._enqueue_dlq(body, payload.output_hash, payload.block)
        return ok

    def _http_post(self, body: bytes, output_hash: str, block: int) -> bool:
        try:
            req = urllib.request.Request(
                self._url,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                self._log(output_hash, block, "ok", resp.status)
                logger.info("Relay submitted | output_hash=%s | status=%d", output_hash[:16], resp.status)
                return True
        except urllib.error.HTTPError as exc:
            self._log(output_hash, block, f"http_{exc.code}", exc.code)
            logger.error("Relay HTTP error | status=%d | url=%s", exc.code, self._url)
            return False
        except Exception as exc:
            self._log(output_hash, block, "error", None)
            logger.error("Relay submission failed | %s", exc)
            return False

    # ── Persistence ────────────────────────────────────────────────────────────

    def _open_db(self) -> sqlite3.Connection:
        self._log_db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._log_db), check_same_thread=False)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS relay_log (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                output_hash  TEXT NOT NULL,
                block        INTEGER NOT NULL,
                status       TEXT NOT NULL,
                http_code    INTEGER,
                submitted_at REAL NOT NULL DEFAULT (strftime('%s','now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS relay_dlq (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                output_hash   TEXT NOT NULL,
                block         INTEGER NOT NULL,
                signed_json   TEXT NOT NULL,
                attempts      INTEGER NOT NULL DEFAULT 0,
                next_retry_at REAL NOT NULL DEFAULT 0,
                status        TEXT NOT NULL DEFAULT 'pending',
                created_at    REAL NOT NULL DEFAULT (strftime('%s','now'))
            )
        """)
        conn.commit()
        return conn

    def _log(self, output_hash: str, block: int, status: str, http_code: int | None) -> None:
        try:
            self._db.execute(
                "INSERT INTO relay_log (output_hash, block, status, http_code) VALUES (?,?,?,?)",
                (output_hash, block, status, http_code),
            )
            self._db.commit()
        except Exception as exc:
            logger.warning("Could not write relay log entry: %s", exc)
