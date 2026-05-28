"""Tests for engram/relay — adapter, signer, client (dry-run)."""

import hashlib
import json
import time

import pytest

from engram.relay.adapter import RelayPayload, _stable_scores_json, build_payload
from engram.relay.client import RelayClient
from engram.relay.signer import sign_payload


# ── adapter tests ─────────────────────────────────────────────────────────────

def _sample_scores():
    return (
        {0: 0.95, 1: 0.80, 2: 1.0},
        {0: 120.5, 1: None, 2: 95.0},
        {0: 0.9, 1: 0.7, 2: 1.0},
    )


def test_build_payload_returns_relay_payload():
    recall, latency, proofs = _sample_scores()
    p = build_payload(recall, latency, proofs, netuid=450, block=1000, validator_uid=5)
    assert isinstance(p, RelayPayload)
    assert p.netuid == 450
    assert p.block == 1000
    assert p.schema_version == "engram-v1"


def test_build_payload_scores_include_all_uids():
    recall, latency, proofs = _sample_scores()
    p = build_payload(recall, latency, proofs)
    assert set(p.scores.keys()) == {"0", "1", "2"}


def test_build_payload_output_hash_is_sha256_of_stable_json():
    recall, latency, proofs = _sample_scores()
    p = build_payload(recall, latency, proofs)
    expected = hashlib.sha256(_stable_scores_json(p.scores).encode()).hexdigest()
    assert p.output_hash == expected


def test_build_payload_nonce_format():
    recall, latency, proofs = _sample_scores()
    p = build_payload(recall, latency, proofs, block=42, validator_uid=3)
    parts = p.nonce.split(":")
    assert parts[0] == "42"
    assert parts[1] == "3"
    assert parts[2].isdigit()


def test_output_hash_changes_if_scores_change():
    recall, latency, proofs = _sample_scores()
    p1 = build_payload(recall, latency, proofs)
    recall2 = {**recall, 0: 0.50}
    p2 = build_payload(recall2, latency, proofs)
    assert p1.output_hash != p2.output_hash


def test_payload_to_json_is_deterministic():
    recall, latency, proofs = _sample_scores()
    p = build_payload(recall, latency, proofs, block=10, validator_uid=1)
    j1 = p.to_json()
    j2 = p.to_json()
    assert j1 == j2
    parsed = json.loads(j1)
    assert parsed["schema_version"] == "engram-v1"


def test_stable_scores_json_sorts_by_uid():
    scores = {"2": {"recall": 1.0, "latency_ms": None, "proof_rate": 1.0},
              "0": {"recall": 0.5, "latency_ms": 100.0, "proof_rate": 0.8}}
    j = _stable_scores_json(scores)
    data = json.loads(j)
    assert list(data.keys()) == ["0", "2"]


def test_build_payload_latency_none_preserved():
    recall = {0: 1.0}
    latency = {0: None}
    proofs = {0: 1.0}
    p = build_payload(recall, latency, proofs)
    assert p.scores["0"]["latency_ms"] is None


# ── signer tests ──────────────────────────────────────────────────────────────

class _FakeKeypair:
    ss58_address = "5FakeHotkey"

    def sign(self, msg: bytes) -> bytes:
        return hashlib.sha256(msg).digest()


def test_sign_payload_adds_required_fields():
    recall, latency, proofs = _sample_scores()
    p = build_payload(recall, latency, proofs)
    signed = sign_payload(p, _FakeKeypair())
    assert "validator_hotkey" in signed
    assert "signature" in signed
    assert "signed_hash" in signed


def test_sign_payload_hotkey_matches_keypair():
    recall, latency, proofs = _sample_scores()
    p = build_payload(recall, latency, proofs)
    kp = _FakeKeypair()
    signed = sign_payload(p, kp)
    assert signed["validator_hotkey"] == kp.ss58_address


def test_sign_payload_signed_hash_is_sha256_of_canonical_json():
    recall, latency, proofs = _sample_scores()
    p = build_payload(recall, latency, proofs)
    canonical = json.dumps(p.to_dict(), separators=(",", ":"), sort_keys=True)
    expected = hashlib.sha256(canonical.encode()).hexdigest()
    signed = sign_payload(p, _FakeKeypair())
    assert signed["signed_hash"] == expected


def test_sign_payload_signature_is_hex_prefixed():
    recall, latency, proofs = _sample_scores()
    p = build_payload(recall, latency, proofs)
    signed = sign_payload(p, _FakeKeypair())
    assert signed["signature"].startswith("0x")


# ── relay client tests (dry-run) ─────────────────────────────────────────────

def test_relay_client_dry_run_returns_true(tmp_path):
    client = RelayClient(xeris_url="", log_db=tmp_path / "relay_log.db")
    recall, latency, proofs = _sample_scores()
    result = client.emit(recall, latency, proofs, block=100)
    assert result is True


def test_relay_client_dry_run_logs_entry(tmp_path):
    import sqlite3
    log_path = tmp_path / "relay_log.db"
    client = RelayClient(xeris_url="", log_db=log_path)
    recall, latency, proofs = _sample_scores()
    client.emit(recall, latency, proofs, block=200)
    conn = sqlite3.connect(str(log_path))
    rows = conn.execute("SELECT status, block FROM relay_log").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "dry_run"
    assert rows[0][1] == 200


def test_relay_client_from_env_dry_run(monkeypatch, tmp_path):
    monkeypatch.delenv("XERIS_RELAY_URL", raising=False)
    client = RelayClient.from_env(keypair=None)
    assert not client._enabled


def test_relay_client_disabled_via_env(monkeypatch, tmp_path):
    monkeypatch.setenv("XERIS_RELAY_URL", "http://example.com/relay")
    monkeypatch.setenv("ENGRAM_RELAY_ENABLED", "false")
    client = RelayClient.from_env()
    assert not client._enabled


# ── DLQ tests ─────────────────────────────────────────────────────────────────

def _failing_client(tmp_path):
    """Client pointed at a URL that will always refuse connections."""
    return RelayClient(
        xeris_url="http://127.0.0.1:19999/relay",
        log_db=tmp_path / "relay_log.db",
        timeout=0.5,
    )


def test_failed_submission_enters_dlq(tmp_path):
    import sqlite3
    client = _failing_client(tmp_path)
    recall, latency, proofs = _sample_scores()
    result = client.emit(recall, latency, proofs, block=1)
    assert result is False
    conn = sqlite3.connect(str(tmp_path / "relay_log.db"))
    rows = conn.execute("SELECT status FROM relay_dlq").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "pending"


def test_dlq_entry_has_correct_block(tmp_path):
    import sqlite3
    client = _failing_client(tmp_path)
    recall, latency, proofs = _sample_scores()
    client.emit(recall, latency, proofs, block=42)
    conn = sqlite3.connect(str(tmp_path / "relay_log.db"))
    row = conn.execute("SELECT block, attempts FROM relay_dlq").fetchone()
    assert row[0] == 42
    assert row[1] == 0


def test_dlq_retry_succeeds_on_next_emit(tmp_path, monkeypatch):
    """Simulate: first emit fails → DLQ enqueued → second emit delivers DLQ entry."""
    import sqlite3
    client = _failing_client(tmp_path)
    recall, latency, proofs = _sample_scores()
    client.emit(recall, latency, proofs, block=10)

    # Force next_retry_at into the past so flush picks it up
    client._db.execute("UPDATE relay_dlq SET next_retry_at=0")
    client._db.commit()

    # Patch _http_post to succeed
    monkeypatch.setattr(client, "_http_post", lambda body, oh, blk: True)
    client._flush_dlq()

    conn = sqlite3.connect(str(tmp_path / "relay_log.db"))
    row = conn.execute("SELECT status FROM relay_dlq").fetchone()
    assert row[0] == "delivered"


def test_dlq_abandoned_after_max_attempts(tmp_path):
    import sqlite3
    from engram.relay.client import _MAX_ATTEMPTS
    client = _failing_client(tmp_path)
    recall, latency, proofs = _sample_scores()
    client.emit(recall, latency, proofs, block=99)

    # Drive attempts to the limit
    client._db.execute(
        "UPDATE relay_dlq SET attempts=?, next_retry_at=0",
        (_MAX_ATTEMPTS - 1,),
    )
    client._db.commit()
    client._flush_dlq()

    conn = sqlite3.connect(str(tmp_path / "relay_log.db"))
    row = conn.execute("SELECT status, attempts FROM relay_dlq").fetchone()
    assert row[0] == "abandoned"
    assert row[1] == _MAX_ATTEMPTS


def test_dlq_not_flushed_before_retry_window(tmp_path):
    import sqlite3
    client = _failing_client(tmp_path)
    recall, latency, proofs = _sample_scores()
    client.emit(recall, latency, proofs, block=5)

    # next_retry_at is in the future — flush should not touch the entry
    client._db.execute("UPDATE relay_dlq SET next_retry_at=?", (time.time() + 9999,))
    client._db.commit()
    client._flush_dlq()

    conn = sqlite3.connect(str(tmp_path / "relay_log.db"))
    row = conn.execute("SELECT attempts FROM relay_dlq").fetchone()
    assert row[0] == 0  # untouched


# ── status() tests ────────────────────────────────────────────────────────────

def test_status_dry_run_defaults(tmp_path):
    client = RelayClient(xeris_url="", log_db=tmp_path / "relay_log.db")
    s = client.status()
    assert s["enabled"] is False
    assert s["dlq_pending"] == 0
    assert s["dlq_abandoned"] == 0
    assert s["total_ok"] == 0
    assert s["last_ok_hash"] is None


def test_status_reflects_dlq_pending(tmp_path):
    client = _failing_client(tmp_path)
    recall, latency, proofs = _sample_scores()
    client.emit(recall, latency, proofs, block=1)
    s = client.status()
    assert s["dlq_pending"] == 1
    assert s["total_failed"] >= 1


def test_status_dry_run_increments_total_ok(tmp_path, monkeypatch):
    client = RelayClient(xeris_url="", log_db=tmp_path / "relay_log.db")
    recall, latency, proofs = _sample_scores()
    client.emit(recall, latency, proofs)
    client.emit(recall, latency, proofs)
    # dry_run entries are not counted as "ok"
    s = client.status()
    assert s["total_ok"] == 0
