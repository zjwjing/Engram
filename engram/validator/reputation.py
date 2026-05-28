"""
Engram Validator — Persistent Miner Reputation Store

Tracks per-miner reliability across validator restarts. Each scoring round
updates the store; the stored EMA score (composite of recall, latency, proof
rate) is used to weight replica assignment toward more reliable miners.

Schema (SQLite):
    miner_reputation
        uid          INTEGER PRIMARY KEY
        hotkey       TEXT
        ema_score    REAL     — exponential moving average composite score
        avg_recall   REAL     — rolling average recall@K
        avg_proof_rate REAL   — rolling average proof success rate
        avg_latency_ms REAL   — rolling average latency (NULL = no data)
        rounds_scored INTEGER — total rounds this miner participated in
        first_seen_at REAL
        last_seen_at  REAL

EMA uses alpha=0.15 (each round counts for 15%, history for 85%) so a single
bad round doesn't crater a reliable miner, and a miner must sustain improvement
to climb the reputation table.
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_DEFAULT_DB = Path(os.getenv("REPUTATION_DB_PATH", "data/miner_reputation.db"))
_EMA_ALPHA  = 0.15   # weight on newest observation


@dataclass
class MinerReputation:
    uid: int
    hotkey: str
    ema_score: float
    avg_recall: float
    avg_proof_rate: float
    avg_latency_ms: Optional[float]
    rounds_scored: int
    first_seen_at: float
    last_seen_at: float

    @property
    def reliability(self) -> float:
        """
        Reliability index in [0, 1].

        Blends EMA composite score with longevity: a miner with 50+ rounds
        scored gets full weight; fewer rounds get a confidence discount.
        This prevents fresh miners from immediately displacing veterans.
        """
        confidence = min(1.0, self.rounds_scored / 50)
        return self.ema_score * confidence


class ReputationStore:
    """
    SQLite-backed store for per-miner reputation.

    Thread-safe for single-process use (validator loop).
    """

    def __init__(self, db_path: Path = _DEFAULT_DB) -> None:
        self._db_path = db_path
        self._conn = self._open_db()

    def _open_db(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS miner_reputation (
                uid             INTEGER PRIMARY KEY,
                hotkey          TEXT NOT NULL DEFAULT '',
                ema_score       REAL NOT NULL DEFAULT 0.0,
                avg_recall      REAL NOT NULL DEFAULT 0.0,
                avg_proof_rate  REAL NOT NULL DEFAULT 0.0,
                avg_latency_ms  REAL,
                rounds_scored   INTEGER NOT NULL DEFAULT 0,
                first_seen_at   REAL NOT NULL,
                last_seen_at    REAL NOT NULL
            )
        """)
        conn.commit()
        return conn

    def update(
        self,
        uid: int,
        *,
        hotkey: str = "",
        recall: float,
        proof_rate: float,
        composite_score: float,
        latency_ms: float | None,
    ) -> MinerReputation:
        """
        Record one scoring round for a miner and return the updated reputation.

        Args:
            uid:             Miner UID.
            hotkey:          SS58 hotkey (for cross-referencing).
            recall:          recall@K score for this round.
            proof_rate:      Storage proof success rate for this round.
            composite_score: Combined score (from compute_miner_score).
            latency_ms:      Query latency, or None if unavailable.
        """
        now = time.time()
        existing = self.get(uid)

        if existing is None:
            rep = MinerReputation(
                uid=uid,
                hotkey=hotkey or "",
                ema_score=composite_score,
                avg_recall=recall,
                avg_proof_rate=proof_rate,
                avg_latency_ms=latency_ms,
                rounds_scored=1,
                first_seen_at=now,
                last_seen_at=now,
            )
        else:
            n = existing.rounds_scored
            # EMA for composite score
            new_ema = _EMA_ALPHA * composite_score + (1 - _EMA_ALPHA) * existing.ema_score
            # Simple rolling average for components (1/(n+1) weight on new obs)
            w = 1.0 / (n + 1)
            new_recall     = (1 - w) * existing.avg_recall + w * recall
            new_proof_rate = (1 - w) * existing.avg_proof_rate + w * proof_rate
            if latency_ms is not None:
                if existing.avg_latency_ms is not None:
                    new_latency: float | None = (1 - w) * existing.avg_latency_ms + w * latency_ms
                else:
                    new_latency = latency_ms
            else:
                new_latency = existing.avg_latency_ms

            rep = MinerReputation(
                uid=uid,
                hotkey=hotkey or existing.hotkey,
                ema_score=new_ema,
                avg_recall=new_recall,
                avg_proof_rate=new_proof_rate,
                avg_latency_ms=new_latency,
                rounds_scored=n + 1,
                first_seen_at=existing.first_seen_at,
                last_seen_at=now,
            )

        self._conn.execute("""
            INSERT INTO miner_reputation
                (uid, hotkey, ema_score, avg_recall, avg_proof_rate,
                 avg_latency_ms, rounds_scored, first_seen_at, last_seen_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(uid) DO UPDATE SET
                hotkey          = excluded.hotkey,
                ema_score       = excluded.ema_score,
                avg_recall      = excluded.avg_recall,
                avg_proof_rate  = excluded.avg_proof_rate,
                avg_latency_ms  = excluded.avg_latency_ms,
                rounds_scored   = excluded.rounds_scored,
                last_seen_at    = excluded.last_seen_at
        """, (
            rep.uid, rep.hotkey, rep.ema_score, rep.avg_recall,
            rep.avg_proof_rate, rep.avg_latency_ms, rep.rounds_scored,
            rep.first_seen_at, rep.last_seen_at,
        ))
        self._conn.commit()
        return rep

    def get(self, uid: int) -> MinerReputation | None:
        row = self._conn.execute(
            "SELECT uid, hotkey, ema_score, avg_recall, avg_proof_rate, "
            "avg_latency_ms, rounds_scored, first_seen_at, last_seen_at "
            "FROM miner_reputation WHERE uid=?",
            (uid,),
        ).fetchone()
        if row is None:
            return None
        return MinerReputation(*row)

    def top_n(self, n: int = 10) -> list[MinerReputation]:
        """Return the n most reliable miners by reliability index."""
        rows = self._conn.execute(
            "SELECT uid, hotkey, ema_score, avg_recall, avg_proof_rate, "
            "avg_latency_ms, rounds_scored, first_seen_at, last_seen_at "
            "FROM miner_reputation ORDER BY ema_score DESC LIMIT ?",
            (n,),
        ).fetchall()
        return [MinerReputation(*r) for r in rows]

    def reliability_map(self) -> dict[int, float]:
        """Return {uid: reliability} for all known miners."""
        rows = self._conn.execute(
            "SELECT uid, ema_score, rounds_scored FROM miner_reputation"
        ).fetchall()
        result = {}
        for uid, ema, rounds in rows:
            confidence = min(1.0, rounds / 50)
            result[uid] = ema * confidence
        return result

    def summary(self) -> str:
        row = self._conn.execute(
            "SELECT COUNT(*), AVG(ema_score), MAX(ema_score) FROM miner_reputation"
        ).fetchone()
        if not row or row[0] == 0:
            return "reputation: no data"
        return (
            f"reputation: miners={row[0]} avg_ema={row[1]:.3f} top_ema={row[2]:.3f}"
        )
