"""
Engram → XERIS Relay — Payload Adapter

Normalises a scored validator round result into the canonical XERIS payload
schema. The schema is designed to be stable so XERIS-side consumers don't
need to handle Engram internals.

Payload wire format (JSON):
    {
        "schema_version": "engram-v1",
        "netuid":         450,
        "block":          <int>,
        "round_ts":       <unix float>,
        "output_hash":    "<sha256 hex of the scores dict>",
        "nonce":          "<block>:<uid>:<ts_ms>",
        "scores": {
            "<uid>": {
                "recall":     <float>,
                "latency_ms": <float | null>,
                "proof_rate": <float>
            },
            ...
        }
    }

output_hash is computed over the stable JSON of the `scores` sub-dict
(keys sorted, floats rounded to 6 dp) so any tampering is detectable.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RelayPayload:
    schema_version: str
    netuid: int
    block: int
    round_ts: float
    output_hash: str
    nonce: str
    scores: dict[str, dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "netuid":         self.netuid,
            "block":          self.block,
            "round_ts":       self.round_ts,
            "output_hash":    self.output_hash,
            "nonce":          self.nonce,
            "scores":         self.scores,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"), sort_keys=True)


def _stable_scores_json(scores: dict[str, dict[str, Any]]) -> str:
    """Produce a deterministic JSON string of the scores dict for hashing."""
    normalised = {}
    for uid, vals in sorted(scores.items(), key=lambda kv: int(kv[0])):
        normalised[str(uid)] = {
            "recall":     round(float(vals.get("recall") or 0.0), 6),
            "latency_ms": round(float(vals["latency_ms"]), 3) if vals.get("latency_ms") is not None else None,
            "proof_rate": round(float(vals.get("proof_rate") or 0.0), 6),
        }
    return json.dumps(normalised, separators=(",", ":"), sort_keys=True)


def build_payload(
    recall_scores: dict[int, float],
    latency_scores: dict[int, float | None],
    proof_rates: dict[int, float],
    *,
    netuid: int = 450,
    block: int = 0,
    validator_uid: int = 0,
) -> RelayPayload:
    """
    Build a RelayPayload from a completed validator scoring round.

    Args:
        recall_scores:  {uid: recall@K score}
        latency_scores: {uid: latency_ms | None}
        proof_rates:    {uid: proof success rate 0..1}
        netuid:         Bittensor subnet UID (default 450).
        block:          Current Bittensor block number.
        validator_uid:  Validator's own UID (used in nonce).
    """
    all_uids = set(recall_scores) | set(latency_scores) | set(proof_rates)
    scores: dict[str, dict[str, Any]] = {}
    for uid in sorted(all_uids):
        scores[str(uid)] = {
            "recall":     recall_scores.get(uid, 0.0),
            "latency_ms": latency_scores.get(uid),
            "proof_rate": proof_rates.get(uid, 0.0),
        }

    output_hash = hashlib.sha256(_stable_scores_json(scores).encode()).hexdigest()
    ts_ms = int(time.time() * 1000)
    nonce = f"{block}:{validator_uid}:{ts_ms}"

    return RelayPayload(
        schema_version="engram-v1",
        netuid=netuid,
        block=block,
        round_ts=time.time(),
        output_hash=output_hash,
        nonce=nonce,
        scores=scores,
    )
