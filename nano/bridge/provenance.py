"""Optional Protocol-C provenance adapter for the risk-gate bridge.

This is a **host-integration concern, not a language feature**. A `.nano`
strategy author has no syntax that reaches this module and no way to invoke
or bypass it — it wraps whatever ``DecisionGate`` a host platform hands to
``NanoBridge``, entirely outside the IR and the compiler. Nano's contract
(strategies propose, gates decide) already gives every decision a recorded
reason; this module lets a production deployment additionally bind that
decision to a non-repudiable, tamper-evident signature via Protocol-C
(https://github.com/AetherAI3/PROTOCOL-C) — useful wherever a decision needs
to be provable to a third party after the fact, not just logged for
internal debugging.

Requires the optional ``aether-protocol-c`` package
(``pip install aether-protocol-c``, or this project's ``provenance`` extra).
Constructing ``ProvenanceGate`` without it installed raises
``ProtocolCUnavailable`` with install instructions — it never silently
degrades to unsigned logging.

Determinism note: wrapping a ``DecisionGate`` here does not break bit-identical
replay of a ``BridgeResult`` (Milestone 5's acceptance test). The returned
``Decision`` is the inner engine's decision, unchanged; the signature and
audit-log entry are a side channel the bridge never sees. Those side-channel
records carry a real wall-clock timestamp and fresh entropy by design — that
is what makes a receipt trustworthy evidence of *when* a decision happened,
as opposed to a deterministic replay artifact.

See ``docs/internal-protocol-c-integration.md`` for where a production
deployment would actually wire this in, and ``examples/provenance_signing_demo.py``
for a runnable walkthrough.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Union

from nano.bridge.ats_bridge import Decision, DecisionGate
from nano.runtime.effects import Intent
from nano.runtime.interpreter import MarketFrame

try:
    import aether_protocol_c as _protocol_c

    PROTOCOL_C_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised via the availability guard test
    _protocol_c = None
    PROTOCOL_C_AVAILABLE = False


class ProtocolCUnavailable(ImportError):
    """The optional ``aether-protocol-c`` package is not installed."""


class ProvenanceError(Exception):
    """A commitment failed self-verification immediately after signing."""


_INSTALL_MESSAGE = (
    "ProvenanceGate requires the optional 'aether-protocol-c' package. "
    "Install with: pip install aether-protocol-c "
    "(source: https://github.com/AetherAI3/PROTOCOL-C)"
)

AccountStateSource = Union[Mapping[str, Any], Callable[[], Mapping[str, Any]], None]


@dataclass(frozen=True)
class ProvenanceReceipt:
    """A signed, independently re-verifiable record that a disposition happened.

    ``commitment``/``signature`` are Protocol-C's native shapes — pass them
    straight to ``aether_protocol_c.verify()`` (or ``verify_receipt`` below)
    to re-check the signature with no secret material involved.
    """

    order_id: str
    commitment: dict
    signature: dict
    verified: bool

    def to_dict(self) -> dict:
        return {
            "order_id": self.order_id,
            "commitment": self.commitment,
            "signature": self.signature,
            "verified": self.verified,
        }


class ProvenanceGate:
    """Wraps a ``DecisionGate`` so every disposition also produces a signed receipt.

    Both approvals and rejections are signed and logged — a rejection is
    still a decision, and provenance over "we said no, here's the proof" is
    exactly as valuable as provenance over an approval.
    """

    def __init__(
        self,
        inner: DecisionGate,
        log_path: Union[str, Path],
        *,
        account_state: AccountStateSource = None,
        reasoning_model: str = "nano-bridge",
    ) -> None:
        if not PROTOCOL_C_AVAILABLE:
            raise ProtocolCUnavailable(_INSTALL_MESSAGE)
        self._inner = inner
        self._log_path = Path(log_path)
        self._account_state = account_state
        self._reasoning_model = reasoning_model
        self._nonce = 0
        self._receipts: dict[str, ProvenanceReceipt] = {}

    @property
    def receipts(self) -> Mapping[str, ProvenanceReceipt]:
        """All receipts signed so far, keyed by order id."""
        return dict(self._receipts)

    def receipt_for(self, order_id: str) -> Optional[ProvenanceReceipt]:
        return self._receipts.get(order_id)

    def _resolve_account_state(self, *, timestamp: int) -> dict:
        self._nonce += 1
        if self._account_state is None:
            state: dict = {}
        elif callable(self._account_state):
            state = dict(self._account_state())
        else:
            state = dict(self._account_state)
        state.setdefault("capital", 0.0)
        state.setdefault("equity", 0.0)
        state.setdefault("open_positions", [])
        state.setdefault("risk_used", 0.0)
        state.setdefault("risk_limit", 0.0)
        state.setdefault("timestamp", timestamp)
        # The nonce is this engine instance's own monotonic counter, always
        # authoritative — never taken from caller-supplied state — so two
        # commitments from the same engine can never share a nonce.
        state["nonce"] = self._nonce
        return state

    def decide(self, intent: Intent, *, frame: MarketFrame) -> Decision:
        """Delegate to the inner engine, then sign a receipt over the result.

        The returned ``Decision`` is exactly the inner engine's decision —
        signing never alters, delays, or can veto a disposition.
        """
        decision = self._inner.decide(intent, frame=frame)

        order_id = f"{intent.action}:{intent.asset or '_'}:{intent.timestamp}:{self._nonce + 1}"
        trade_details = {
            "nano_intent": intent.to_dict(),
            "approved": decision.approved,
            "reason": decision.reason,
        }
        account_state = self._resolve_account_state(timestamp=intent.timestamp)

        seed = _protocol_c.get_seed()
        result = _protocol_c.commit(
            seed,
            order_id=order_id,
            trade_details=trade_details,
            account_state=account_state,
            reasoning_text=f"nano decision gate: {decision.reason}",
            reasoning_model=self._reasoning_model,
            log_path=str(self._log_path),
        )

        receipt = ProvenanceReceipt(
            order_id=order_id,
            commitment=result["commitment"],
            signature=result["signature"],
            verified=result["verified"],
        )
        self._receipts[order_id] = receipt

        if not receipt.verified:
            raise ProvenanceError(
                f"Protocol-C commitment for {order_id!r} failed self-verification"
            )

        return decision

    def verify_receipt(self, order_id: str) -> bool:
        """Independently re-verify a stored receipt's signature.

        Uses only the public commitment + signature — no secret material is
        (or ever was) retained; the signing key was destroyed at sign time.
        """
        receipt = self._receipts.get(order_id)
        if receipt is None:
            return False
        return bool(_protocol_c.verify(receipt.commitment, receipt.signature))


# Backward-compatible alias for the pre-decoupling name.
ProvenanceRiskEngine = ProvenanceGate
