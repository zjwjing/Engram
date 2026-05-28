# Engram — Task Backlog

## Security Hardening (ATLAS Threat Model)

### ✅ Done

| Task | Closes | Shipped |
|---|---|---|
| Gaussian DP noise on private namespace embeddings | AML.T0024 vector inversion | `f80a804` |
| `encrypt_raw()` — encrypt media bytes before Arweave upload | AML.T0035 pre-vectorization plaintext | `f80a804` |
| Replace `namespace_key` wire exposure with sr25519 signed challenge | AML.T0043 key-in-transit | `58b312f` |
| Flag-stripping downgrade closed via signature coverage | AML.T0043 downgrade | `58b312f` |
| TLS on miner — `https://api.theengram.space` routes to port 8091 | passive wire sniffing | `58b312f` |
| Arweave storage across full stack (SDK + miner, not just web) | AML.T0035 | `f80a804` |
| Fix event-loop blocking — embed/metagraph calls moved to thread executors | operational resilience | `c5dbbeb` |
| Switch vector store to Qdrant — crash-safe WAL, zero data loss on restart | operational resilience | `c5dbbeb` |
| Timing + payload padding for private namespace queries | AML.T0036 side-channel | current |
| REQUIRE_HOTKEY_SIG defaults to true on mainnet via ENGRAM_ENV | AML.T0043 unsigned bypass | current |
| Weekly miner auto-restart cron (`/etc/cron.d/engram-miner-restart`) | operational resilience | current |
| Shamir K-of-N threshold decryption — `engram/sdk/shamir.py`, `KeyShareStore`, `KeyShareSynapse`/`KeyShareRetrieve`, miner HTTP endpoints | AML.T0010 compromised miner | `60d4c669` |

---

### ✅ Threshold Decryption (K-of-N miners)

Shipped. `engram/sdk/shamir.py` (GF(256) Shamir split/reconstruct), `engram/miner/key_share_store.py` (SQLite share store), `KeyShareSynapse`/`KeyShareRetrieve` protocol synapses, and `/KeyShareSynapse` + `/KeyShareRetrieve` HTTP endpoints in `neurons/miner.py`. Client-side `distribute_key_shares` / `collect_key_shares` in `engram/sdk/client.py`. 23 passing tests.

---

### ✅ Timing / Access-Pattern Side-Channel

Shipped. Private namespace queries now padded to nearest 100ms latency bucket (`_pad_latency`) and nearest 1KB/4KB/16KB/64KB payload bucket (`_pad_payload`) in `neurons/miner.py`. Public queries unaffected (no validator scoring impact).

---

### ✅ Miner Event-Loop Freeze + Memory Loss Prevention

Root cause was two blocking calls on the asyncio event loop (OpenAI embedding + metagraph refresh), not FAISS memory growth. Fixed by moving both to thread executors. Vector store switched from FAISS (in-memory, crash-loses-data) to Qdrant (WAL-backed, crash-safe). Weekly restart cron added as safety net.

---

### ✅ REQUIRE_HOTKEY_SIG Enforcement

Shipped. `ENGRAM_ENV=mainnet` in `.env.miner` causes `REQUIRE_HOTKEY_SIG` to default to `true`. Local dev remains permissive without any env change needed.

---

## Engram → XERIS Relay (Bridge / Intelligence Router)

Validators become AI message buses — Engram subnet outputs forwarded as signed payloads into XERIS mainnet for SageBot consumption.

Architecture: `Engram Subnet → Validator Listener → Relay Adapter → Signed Payload → XERIS Mainnet → SageBot / Agents`

### Phase 1 — Relay Core ✅

- [x] Create `engram/relay/` module
- [x] `engram/relay/adapter.py` — normalize subnet output to XERIS payload schema (include `output_hash`, `netuid`, `block`)
- [x] `engram/relay/client.py` — HTTP/gRPC client that submits signed payload to XERIS endpoint
- [x] `engram/relay/signer.py` — sign payload with validator sr25519 hotkey (reuse existing key infra)
- [x] Add output hash (`sha256(json.dumps(result))`) + Engram block number to every payload to prevent tampering

### Phase 2 — Validator Integration

- [x] Hook `relay.emit()` into `neurons/validator.py` after `set_weights()`
- [x] Nonce (block + uid + timestamp) on every payload to prevent replay attacks
- [x] Log all relay submissions to local SQLite for debugging and audit
- [ ] Finality guard — only relay after 3 block confirmations on Engram side

### Phase 3 — Trust & Reliability

- [x] Dead-letter queue — retry with exponential backoff (10s→300s cap, 10 max attempts), abandoned entries logged as errors, never silently dropped
- [x] `relay_client.status()` — returns dlq_pending, dlq_abandoned, total_ok, last_ok_hash
- [ ] Multi-validator quorum — at least 2 validator signatures before XERIS accepts payload
- [ ] XERIS-side verification — `output_hash` checked against Engram on-chain state

### Phase 4 — SageBot Integration

- [ ] Define XERIS payload schema for each Engram output type (memory, inference, ranking)
- [ ] Wire Dexter MCP tool calls into relay flow
- [ ] x402 auth on relay submissions (XERIS knows who is paying for compute)
- [ ] SageBot receives Engram outputs as `external_intelligence` context

### Phase 5 — Observability

- [ ] Grafana: relay latency, submission success rate, payload volume
- [ ] Alert on 3+ consecutive relay failures
- [ ] Track XERIS acknowledgement vs relay submission timestamp (end-to-end lag)

---

## Infrastructure

### 🔲 theengram.space SSL Certificate

**Priority:** Medium  
**Effort:** 30 minutes

DNS for `theengram.space` resolves to Vercel (216.198.79.65), not the VPS.
Vercel handles TLS automatically. No action needed unless the domain is migrated.

If the domain is pointed at the VPS in the future:
```bash
certbot --nginx -d theengram.space -d www.theengram.space \
  --non-interactive --agree-tos -m careyisabella22@gmail.com --redirect
```

---

### ✅ Weekly Miner Auto-Restart Cron

Shipped. `/etc/cron.d/engram-miner-restart` restarts the miner every Sunday at 04:00 UTC.
