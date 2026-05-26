"""
Engram → XERIS Relay — HTTP Client

Submits signed relay payloads to the XERIS endpoint.

Configuration (environment variables):
    XERIS_RELAY_URL      — XERIS submission endpoint (required to actually relay)
    XERIS_RELAY_TIMEOUT  — request timeout in seconds (default 10)
    ENGRAM_RELAY_ENABLED — set to "false" to disable relay without code changes

When XERIS_RELAY_URL is unset the client operates in dry-run mode: payloads
are built and logged but not submitted. This lets the validator integrate the
relay call safely before XERIS is ready.

Submission log is written to RELAY_LOG_DB (default data/relay_log.db) so
failed submissions can be audited without a full observability stack.
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


class RelayClient:
    """
    Submits scored validator round results to XERIS as signed payloads.

    Args:
        xeris_url:  XERIS relay endpoint. If None or empty, dry-run mode.
        keypair:    ``bt.Keypair`` for signing. If None, payloads are unsigned
                    (only valid in dry-run mode).
        timeout:    HTTP request timeout in seconds.
        log_db:     Path to SQLite submission log.
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
        self._db = self._open_log_db()

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

        Args:
            recall_scores:  {uid: recall@K}
            latency_scores: {uid: latency_ms | None}
            proof_rates:    {uid: proof success rate}
            netuid:         Subnet UID.
            block:          Current block number.
            validator_uid:  Validator's own UID (used in nonce).

        Returns:
            True if submitted (or dry-run), False on HTTP error.
        """
        payload = build_payload(
            recall_scores, latency_scores, proof_rates,
            netuid=netuid, block=block, validator_uid=validator_uid,
        )
        return self._submit(payload)

    # ── Internal ───────────────────────────────────────────────────────────────

    def _submit(self, payload: RelayPayload) -> bool:
        signed: dict[str, Any]
        if self._keypair is not None:
            signed = sign_payload(payload, self._keypair)
        else:
            signed = payload.to_dict()

        body = json.dumps(signed, separators=(",", ":")).encode("utf-8")

        if not self._enabled:
            logger.debug(
                "Relay dry-run | output_hash=%s | miners=%d",
                payload.output_hash[:16],
                len(payload.scores),
            )
            self._log(payload.output_hash, payload.block, "dry_run", None)
            return True

        try:
            req = urllib.request.Request(
                self._url,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                status = resp.status
                logger.info(
                    "Relay submitted | output_hash=%s | status=%d | miners=%d",
                    payload.output_hash[:16], status, len(payload.scores),
                )
                self._log(payload.output_hash, payload.block, "ok", status)
                return True
        except urllib.error.HTTPError as exc:
            logger.error("Relay HTTP error | status=%d | url=%s", exc.code, self._url)
            self._log(payload.output_hash, payload.block, f"http_{exc.code}", exc.code)
            return False
        except Exception as exc:
            logger.error("Relay submission failed | %s", exc)
            self._log(payload.output_hash, payload.block, "error", None)
            return False

    def _open_log_db(self) -> sqlite3.Connection:
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
