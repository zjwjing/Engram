"""
Engram → XERIS Relay

Forwards scored subnet outputs as signed payloads to the XERIS mainnet
so SageBot and other agents can consume Engram memories as external intelligence.

Usage (validator integration — Phase 2):

    from engram.relay import RelayClient
    relay = RelayClient.from_env()
    relay.emit(round_result)
"""

from engram.relay.adapter import RelayPayload, build_payload
from engram.relay.client import RelayClient
from engram.relay.signer import sign_payload

__all__ = ["RelayClient", "RelayPayload", "build_payload", "sign_payload"]
