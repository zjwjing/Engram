"""
Engram → XERIS Relay — Payload Signer

Signs a RelayPayload with the validator's sr25519 hotkey so XERIS can verify
the payload came from a registered Engram validator.

Wire format adds three fields to the payload dict:
    "validator_hotkey": "<SS58>",
    "signature":        "0x<hex sr25519 sig>",
    "signed_hash":      "<sha256 of the canonical payload JSON>"

The signature is over signed_hash (UTF-8 bytes) so XERIS only needs to
recompute the sha256 to verify — it never needs the full payload JSON.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from engram.relay.adapter import RelayPayload


def sign_payload(payload: RelayPayload, keypair: Any) -> dict[str, Any]:
    """
    Sign a RelayPayload with a Bittensor keypair.

    Args:
        payload: The relay payload to sign.
        keypair: A ``bt.Keypair`` instance with a loaded private key.

    Returns:
        dict ready for JSON serialisation and HTTP submission, with
        ``validator_hotkey``, ``signature``, and ``signed_hash`` fields added.
    """
    body = payload.to_dict()
    canonical = json.dumps(body, separators=(",", ":"), sort_keys=True)
    signed_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    sig = "0x" + keypair.sign(signed_hash.encode("utf-8")).hex()
    return {
        **body,
        "validator_hotkey": keypair.ss58_address,
        "signature":        sig,
        "signed_hash":      signed_hash,
    }
