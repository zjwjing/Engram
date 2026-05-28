"""Tests for engram/validator/reputation.py"""

import time

import pytest

from engram.validator.reputation import MinerReputation, ReputationStore, _EMA_ALPHA


@pytest.fixture
def store(tmp_path):
    return ReputationStore(db_path=tmp_path / "rep.db")


# ── first update creates record ───────────────────────────────────────────────

def test_new_miner_gets_record(store):
    rep = store.update(0, recall=0.8, proof_rate=0.9, composite_score=0.75, latency_ms=120.0)
    assert rep.uid == 0
    assert rep.rounds_scored == 1
    assert rep.ema_score == pytest.approx(0.75)
    assert rep.avg_recall == pytest.approx(0.8)
    assert rep.avg_proof_rate == pytest.approx(0.9)
    assert rep.avg_latency_ms == pytest.approx(120.0)


def test_new_miner_with_no_latency(store):
    rep = store.update(1, recall=1.0, proof_rate=1.0, composite_score=1.0, latency_ms=None)
    assert rep.avg_latency_ms is None


def test_hotkey_stored(store):
    rep = store.update(2, hotkey="5FakeHotkey", recall=0.5, proof_rate=0.5, composite_score=0.5, latency_ms=None)
    assert rep.hotkey == "5FakeHotkey"


# ── EMA and rolling averages ──────────────────────────────────────────────────

def test_ema_score_converges_toward_new_observations(store):
    store.update(0, recall=0.0, proof_rate=0.0, composite_score=0.0, latency_ms=None)
    rep = store.update(0, recall=1.0, proof_rate=1.0, composite_score=1.0, latency_ms=None)
    expected_ema = _EMA_ALPHA * 1.0 + (1 - _EMA_ALPHA) * 0.0
    assert rep.ema_score == pytest.approx(expected_ema)


def test_rolling_avg_recall(store):
    store.update(0, recall=0.0, proof_rate=0.0, composite_score=0.0, latency_ms=None)
    rep = store.update(0, recall=1.0, proof_rate=0.0, composite_score=0.0, latency_ms=None)
    # 1 existing round, w = 1/(1+1) = 0.5
    assert rep.avg_recall == pytest.approx(0.5)


def test_rounds_scored_increments(store):
    for i in range(5):
        rep = store.update(0, recall=0.5, proof_rate=0.5, composite_score=0.5, latency_ms=100.0)
    assert rep.rounds_scored == 5


def test_latency_updates_rolling_average(store):
    store.update(0, recall=0.5, proof_rate=0.5, composite_score=0.5, latency_ms=100.0)
    rep = store.update(0, recall=0.5, proof_rate=0.5, composite_score=0.5, latency_ms=200.0)
    assert rep.avg_latency_ms == pytest.approx(150.0)


def test_none_latency_does_not_reset_existing(store):
    store.update(0, recall=0.5, proof_rate=0.5, composite_score=0.5, latency_ms=100.0)
    rep = store.update(0, recall=0.5, proof_rate=0.5, composite_score=0.5, latency_ms=None)
    assert rep.avg_latency_ms == pytest.approx(100.0)


# ── reliability index ─────────────────────────────────────────────────────────

def test_reliability_discounts_new_miners(store):
    rep = store.update(0, recall=1.0, proof_rate=1.0, composite_score=1.0, latency_ms=None)
    # 1 round → confidence = 1/50 = 0.02
    assert rep.reliability == pytest.approx(1.0 * (1 / 50))


def test_reliability_reaches_full_at_50_rounds(store):
    for _ in range(50):
        rep = store.update(0, recall=1.0, proof_rate=1.0, composite_score=1.0, latency_ms=None)
    assert rep.reliability == pytest.approx(rep.ema_score, abs=1e-4)


def test_reliability_map_includes_all_miners(store):
    store.update(0, recall=0.5, proof_rate=0.5, composite_score=0.5, latency_ms=None)
    store.update(1, recall=0.8, proof_rate=0.8, composite_score=0.8, latency_ms=None)
    m = store.reliability_map()
    assert set(m.keys()) == {0, 1}


# ── persistence across instances ─────────────────────────────────────────────

def test_persists_across_instances(tmp_path):
    db = tmp_path / "rep.db"
    s1 = ReputationStore(db_path=db)
    s1.update(7, recall=0.9, proof_rate=0.9, composite_score=0.9, latency_ms=50.0)
    s2 = ReputationStore(db_path=db)
    rep = s2.get(7)
    assert rep is not None
    assert rep.rounds_scored == 1
    assert rep.avg_recall == pytest.approx(0.9)


def test_get_nonexistent_returns_none(store):
    assert store.get(999) is None


# ── top_n ─────────────────────────────────────────────────────────────────────

def test_top_n_returns_best_miners(store):
    for uid, score in [(0, 0.3), (1, 0.9), (2, 0.6)]:
        store.update(uid, recall=score, proof_rate=score, composite_score=score, latency_ms=None)
    top = store.top_n(2)
    assert top[0].uid == 1
    assert top[1].uid == 2


def test_top_n_limited(store):
    for uid in range(5):
        store.update(uid, recall=0.5, proof_rate=0.5, composite_score=0.5, latency_ms=None)
    assert len(store.top_n(3)) == 3


# ── summary ───────────────────────────────────────────────────────────────────

def test_summary_empty(store):
    assert "no data" in store.summary()


def test_summary_populated(store):
    store.update(0, recall=0.8, proof_rate=0.8, composite_score=0.8, latency_ms=None)
    s = store.summary()
    assert "miners=1" in s
    assert "avg_ema" in s
