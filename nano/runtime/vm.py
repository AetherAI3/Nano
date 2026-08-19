"""The Nano VM — the single evaluator for compiled modules.

Both IR versions run here. v1.0 modules arrive directly; baseline graphs arrive
via ``StrategyGraph.to_module()``. Having one evaluator is the point: two would
eventually disagree, and the disagreement would surface as a backtest that no
longer matches production.

Evaluation is in two stages, and the split is what keeps it honest:

**Stage 1 — derive every series once, over the whole frame.** Indicators,
arithmetic, comparisons, and offsets are computed for all bars up front. Each
kernel sees only the data it is entitled to (``nano/indicators/compute.py`` never
reads forward), and a warm-up bar yields absence rather than a fabricated number.

**Stage 2 — walk the schedule and sample.** At each tick the rule's condition is
read *at that bar*. A condition that is absent — because something upstream has
not warmed up — is not true, so no intent is proposed from a bar whose inputs did
not exist. This is why `warmup` travels in the IR: a replay can report how many
bars it discarded instead of quietly counting them as no-signal.

Purity is the contract the reference interpreter has always held: identical module
plus identical frame gives an identical result, bit for bit. No ambient clock, no
ambient randomness, no I/O.

Reasoning calls are the one stochastic thing a module can contain, and the VM
contains rather than solves that. It never constructs a provider — one is injected
or `infer` yields no value at all — so **the VM is exactly as deterministic as the
provider it was handed.** A provider that replays a recorded transcript makes a run
bit-reproducible; one that calls a live model does not, and no amount of care here
would change that. Recording transcripts is the host's job, and this package ships
no recorder: see ``ReasoningProvider`` below for the interface a host implements.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import (
    Any,
    Dict,
    List,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    Union,
)

from ..indicators import evaluate as evaluate_indicator
from ..indicators.registry import lookup as lookup_indicator
from ..ir.module import IRNode, NanoModule
from .effects import Intent, LogEntry
from .interpreter import MarketFrame, RuntimeError_
from .risk import ACTUATING_ACTIONS, RiskGate
from .scheduler import ticks

Cell = Optional[Union[float, bool]]
Series = Tuple[Any, ...]
# A node's value is either a whole series or a single compile-time scalar.
Value = Union[Series, float, int, bool, str, None]


class ReasoningProvider(Protocol):
    """Supplies the result of one `infer` call against a declared signature.

    Implemented by a host (Aether Cloud's reasoning gateway, ATS's llmre) and, in
    replay, by ``nano.agents.RecordedProvider``. The VM never constructs a
    provider itself, so a module cannot reach a model the caller did not hand it.
    """

    def infer(
        self, signature: str, inputs: Mapping[str, Cell], *, timestamp: int
    ) -> Mapping[str, Cell]: ...


@dataclass(frozen=True)
class Escalation:
    """A recorded hand-off back to a reasoning layer."""

    target: str
    timestamp: int
    is_agent: bool
    reason: str

    def to_dict(self) -> dict:
        return {
            "escalate": self.target,
            "timestamp": self.timestamp,
            "isAgent": self.is_agent,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ModuleResult:
    """One run's complete account: intents, escalations, and the audit log."""

    intents: Tuple[Intent, ...]
    escalations: Tuple[Escalation, ...]
    log: Tuple[LogEntry, ...]
    warmup_bars_skipped: int = 0

    def to_dict(self) -> dict:
        return {
            "intents": [i.to_dict() for i in self.intents],
            "escalations": [e.to_dict() for e in self.escalations],
            "log": [entry.to_dict() for entry in self.log],
            "warmup_bars_skipped": self.warmup_bars_skipped,
        }


# ---------------------------------------------------------------------------
# elementwise helpers
# ---------------------------------------------------------------------------


def _as_series(value: Value, length: int) -> Series:
    """Broadcast a scalar across every bar; pass a series through unchanged."""
    if isinstance(value, tuple):
        return value
    return (value,) * length


def _zip_apply(left: Series, right: Series, fn) -> Series:
    out: List[Cell] = []
    for index in range(min(len(left), len(right))):
        a, b = left[index], right[index]
        out.append(None if a is None or b is None else fn(a, b))
    return tuple(out)


def _map_apply(values: Series, fn) -> Series:
    return tuple(None if v is None else fn(v) for v in values)


def _divide(a: Cell, b: Cell) -> Cell:
    # A zero divisor yields absence rather than an exception. A strategy dividing
    # by a series cannot know in advance that one bar will be zero, and aborting a
    # whole backtest over one bar is worse than that bar having no value.
    return None if float(b) == 0.0 else float(a) / float(b)


def _modulo(a: Cell, b: Cell) -> Cell:
    return None if float(b) == 0.0 else float(a) % float(b)


_BINARY_KERNELS = {
    "arith.add": lambda a, b: float(a) + float(b),
    "arith.sub": lambda a, b: float(a) - float(b),
    "arith.mul": lambda a, b: float(a) * float(b),
    "arith.div": _divide,
    "arith.mod": _modulo,
    "compare.lt": lambda a, b: a < b,
    "compare.le": lambda a, b: a <= b,
    "compare.gt": lambda a, b: a > b,
    "compare.ge": lambda a, b: a >= b,
    "compare.eq": lambda a, b: a == b,
    "compare.ne": lambda a, b: a != b,
    "logic.and": lambda a, b: bool(a) and bool(b),
    "logic.or": lambda a, b: bool(a) or bool(b),
}


def _field_of(cell: Any, name: str) -> Cell:
    """Read one field from a reasoning result, tolerating absence."""
    if isinstance(cell, Mapping):
        value = cell.get(name)
        return value if value is None or isinstance(value, (int, float, bool)) else None
    return None


# ---------------------------------------------------------------------------
# the machine
# ---------------------------------------------------------------------------


@dataclass
class _Machine:
    module: NanoModule
    frame: MarketFrame
    provider: Optional[ReasoningProvider] = None
    values: Dict[str, Value] = field(default_factory=dict)
    log: List[LogEntry] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.length = len(self.frame.timestamps)
        self.index = self.module.index()
        self._params = {p.name: p.value for p in self.module.params}
        self._signatures = {
            node.attrs["name"]: node.attrs
            for node in self.module.of_op("ai.signature")
        }
        # The risk block's whole runtime lives in `.risk`; a module without one
        # gets an inert gate that adds no decision and no log entry.
        self.risk = RiskGate.for_module(self.module, self.frame)

    # -- stage 1: derive every series --------------------------------------

    def derive(self) -> None:
        """Evaluate every node once, in declaration order.

        Declaration order is a valid evaluation order because the loader proved
        the graph is topologically sorted — every operand is already computed by
        the time a node is reached, so there is no recursion and no cycle check.
        """
        for node in self.module.nodes:
            self.values[node.id] = self._evaluate(node)

    def _operands(self, node: IRNode) -> List[Value]:
        return [self.values[i] for i in node.inputs]

    def _evaluate(self, node: IRNode) -> Value:
        op = node.op

        if op == "const":
            return node.attrs["value"]
        if op == "param.ref":
            return self._params.get(node.attrs["name"])
        if op in ("input.ref", "feed.signal"):
            return self._read_frame(node.attrs["name"])
        if op == "builtin.confidence":
            return self._confidence_series()
        if op == "let":
            return self._operands(node)[0]

        if op == "series.index":
            target = _as_series(self._operands(node)[0], self.length)
            offset = int(node.attrs["offset"])
            # Bars before the offset have no history to read, so they are absent
            # rather than clamped to the first bar -- clamping would invent data.
            return tuple(
                target[i - offset] if i >= offset else None
                for i in range(self.length)
            )

        if op == "indicator":
            return self._evaluate_indicator(node)

        if op == "arith.neg":
            return _map_apply(
                _as_series(self._operands(node)[0], self.length), lambda v: -float(v)
            )
        if op == "logic.not":
            return _map_apply(
                _as_series(self._operands(node)[0], self.length),
                lambda v: not bool(v),
            )
        if op in _BINARY_KERNELS:
            left, right = self._operands(node)
            return _zip_apply(
                _as_series(left, self.length),
                _as_series(right, self.length),
                _BINARY_KERNELS[op],
            )

        if op == "ai.infer":
            return self._evaluate_infer(node)
        if op == "record.field":
            return self._evaluate_record_field(node)

        # schedule / block / rule / route / intent.emit / llmre.escalate / agent /
        # ai.signature / risk.limits carry no series value: they are control flow,
        # effects, or declarations, and stage 2 handles them.
        return None

    def _read_frame(self, name: str) -> Series:
        if name not in self.frame.signals:
            raise RuntimeError_(f"Signal {name!r} not present in market frame")
        return tuple(
            None if v is None else float(v) for v in self.frame.signals[name]
        )

    def _confidence_series(self) -> Series:
        """Resolve the `confidence` builtin for every bar.

        Confidence is *injected*, like time and entropy. Three sources, in order:
        a `confidence` series on the frame; the `confidence` output of a reasoning
        call if the module makes one; then 1.0 for a module that never expresses
        doubt. Nothing here samples an ambient value.
        """
        if "confidence" in self.frame.signals:
            return self._read_frame("confidence")
        for node in self.module.of_op("ai.infer"):
            resolved = self.values.get(node.id)
            if isinstance(resolved, tuple):
                return tuple(_field_of(cell, "confidence") for cell in resolved)
        return (1.0,) * self.length

    def _evaluate_indicator(self, node: IRNode) -> Series:
        name = node.attrs["name"]
        spec = lookup_indicator(name)
        if spec is None:  # pragma: no cover - the loader validated the name
            raise RuntimeError_(f"Unknown indicator {name!r}")

        operands = [_as_series(v, self.length) for v in self._operands(node)]

        # Rebuild the original argument order: periods were folded into attrs at
        # compile time, so they are spliced back into their declared positions.
        arguments: List[Any] = []
        period_positions = set(spec.period_indices)
        operand_iter = iter(operands)
        period_iter = iter(list(node.attrs.get("periods", [])))
        for position in range(spec.arity):
            if position in period_positions:
                arguments.append(next(period_iter))
            else:
                arguments.append(next(operand_iter))
        return evaluate_indicator(name, arguments, length=self.length)

    def _evaluate_infer(self, node: IRNode) -> Series:
        """Call the injected provider once per bar and keep the whole result.

        With no provider the result is absent for every bar. That is deliberate: a
        module that needs reasoning and was given none should produce no signal,
        not a default one.
        """
        signature = node.attrs["signature"]
        first_timestamp = self.frame.timestamps[0] if self.frame.timestamps else 0

        if self.provider is None:
            self.log.append(
                LogEntry(
                    event="infer.skipped",
                    timestamp=first_timestamp,
                    detail=f"{signature}: no reasoning provider supplied",
                )
            )
            return (None,) * self.length

        declared = self._signatures.get(signature, {})
        names = [f["name"] for f in declared.get("inputs", [])]
        operands = [_as_series(v, self.length) for v in self._operands(node)]

        out: List[Any] = []
        for bar in range(self.length):
            payload = {
                name: (operands[position][bar] if position < len(operands) else None)
                for position, name in enumerate(names)
            }
            result = self.provider.infer(
                signature, payload, timestamp=self.frame.timestamps[bar]
            )
            out.append(dict(result))
            self.log.append(
                LogEntry(
                    event="infer.called",
                    timestamp=self.frame.timestamps[bar],
                    detail=f"{signature} -> {sorted(result)}",
                )
            )
        return tuple(out)

    def _evaluate_record_field(self, node: IRNode) -> Series:
        field_name = node.attrs["field"]
        target = _as_series(self._operands(node)[0], self.length)
        return tuple(_field_of(cell, field_name) for cell in target)

    # -- stage 2: walk the schedule ----------------------------------------

    def run(self) -> ModuleResult:
        first_timestamp = self.frame.timestamps[0] if self.frame.timestamps else 0
        self.log.append(
            LogEntry(
                event="module.loaded",
                timestamp=first_timestamp,
                detail=(
                    f"{self.module.name} tier={self.module.tier} "
                    f"effects={list(self.module.effects)} "
                    f"warmup={self.module.warmup}"
                ),
            )
        )
        self.log.extend(self.risk.declaration_log(first_timestamp))
        self.derive()

        intents: List[Intent] = []
        escalations: List[Escalation] = []
        skipped = 0

        for entry in self.module.entries:
            node = self.index[entry]
            if node.op == "rule":
                skipped += self._run_rule(node, intents, escalations)
            elif node.op == "route":
                self._run_route(node, escalations)

        return ModuleResult(
            intents=tuple(intents),
            escalations=tuple(escalations),
            log=tuple(self.log),
            warmup_bars_skipped=skipped,
        )

    def _run_rule(
        self, node: IRNode, intents: List[Intent], escalations: List[Escalation]
    ) -> int:
        schedule_id, condition_id, then_id = node.inputs[0], node.inputs[1], node.inputs[2]
        else_id = node.inputs[3] if len(node.inputs) > 3 else None

        interval = self.index[schedule_id].attrs["interval"]
        condition = _as_series(self.values.get(condition_id), self.length)
        skipped = 0

        for bar, timestamp in ticks(interval, self.frame.timestamps):
            observed = condition[bar] if bar < len(condition) else None
            if observed is None:
                # Absent, not false: something upstream has not warmed up. Counted
                # so a replay can report how many bars it discarded.
                skipped += 1
                self.log.append(
                    LogEntry(
                        event="condition.unwarmed",
                        timestamp=timestamp,
                        detail=f"{condition_id} has no value at bar {bar}",
                    )
                )
                continue

            passed = bool(observed)
            self.log.append(
                LogEntry(
                    event="condition.evaluated",
                    timestamp=timestamp,
                    detail=f"{condition_id} -> {passed}",
                )
            )
            branch = then_id if passed else else_id
            if branch is not None:
                self._run_block(branch, timestamp, bar, intents, escalations)
        return skipped

    def _run_block(
        self,
        block_id: str,
        timestamp: int,
        bar: int,
        intents: List[Intent],
        escalations: List[Escalation],
    ) -> None:
        for statement_id in self.index[block_id].inputs:
            statement = self.index[statement_id]

            if statement.op == "intent.emit":
                intent = Intent(
                    action=statement.attrs["action"],
                    timestamp=timestamp,
                    asset=statement.attrs.get("asset"),
                    confidence=statement.attrs.get("confidence"),
                )
                # A breached limit removes the proposal and says why. It never
                # substitutes a different intent: the host still decides on
                # everything that survives, and sees nothing that did not.
                # The host's order count describes the state at this bar. Add
                # only actuating intents that already survived at the same
                # timestamp so two rules cannot overbook one frame, while a
                # later bar remains governed by its own host measurement.
                emitted_capacity = sum(
                    accepted.timestamp == timestamp
                    and accepted.action in ACTUATING_ACTIONS
                    for accepted in intents
                )
                violations = self.risk.review(
                    intent, bar, emitted_capacity=emitted_capacity
                )
                if violations:
                    self.log.extend(self.risk.suppression_log(intent, violations))
                    continue
                intents.append(intent)
                self.log.append(
                    LogEntry(
                        event="intent.emitted",
                        timestamp=timestamp,
                        detail=f"{intent.action} asset={intent.asset}",
                    )
                )
                continue

            if statement.op == "llmre.escalate":
                escalations.append(
                    self._escalate(statement, timestamp, reason="rule body")
                )
                continue

            if statement.op == "rule":
                # A nested rule shares the enclosing schedule, so it is evaluated
                # at this bar rather than re-walked over the whole timeline.
                self._run_nested_rule(statement, timestamp, bar, intents, escalations)

    def _run_nested_rule(
        self,
        node: IRNode,
        timestamp: int,
        bar: int,
        intents: List[Intent],
        escalations: List[Escalation],
    ) -> None:
        condition = _as_series(self.values.get(node.inputs[1]), self.length)
        observed = condition[bar] if bar < len(condition) else None
        if observed is None:
            return
        if bool(observed):
            branch: Optional[str] = node.inputs[2]
        else:
            branch = node.inputs[3] if len(node.inputs) > 3 else None
        if branch is not None:
            self._run_block(branch, timestamp, bar, intents, escalations)

    def _run_route(self, node: IRNode, escalations: List[Escalation]) -> None:
        """Evaluate a confidence route over every bar of the frame.

        A route has no schedule of its own: it guards a named execution path, so
        it is checked at every bar and escalates on the bars where the guard does
        not hold.
        """
        condition = _as_series(self.values.get(node.inputs[0]), self.length)
        escalate_node = self.index[node.inputs[1]]
        name = node.attrs["name"]

        for bar, timestamp in enumerate(self.frame.timestamps):
            observed = condition[bar] if bar < len(condition) else None
            if observed is None or not bool(observed):
                reason = (
                    f"route {name}: guard not satisfied"
                    if observed is not None
                    else f"route {name}: guard has no value"
                )
                escalations.append(
                    self._escalate(escalate_node, timestamp, reason=reason)
                )
                continue
            self.log.append(
                LogEntry(
                    event="route.executed",
                    timestamp=timestamp,
                    detail=f"{name} -> {node.attrs['execute']}",
                )
            )

    def _escalate(self, node: IRNode, timestamp: int, *, reason: str) -> Escalation:
        escalation = Escalation(
            target=node.attrs["target"],
            timestamp=timestamp,
            is_agent=bool(node.attrs.get("isAgent", False)),
            reason=reason,
        )
        self.log.append(
            LogEntry(
                event="llmre.escalated",
                timestamp=timestamp,
                detail=f"{escalation.target}: {reason}",
            )
        )
        return escalation


def run_module(
    module: NanoModule,
    frame: MarketFrame,
    *,
    provider: Optional[ReasoningProvider] = None,
    validate: bool = True,
) -> ModuleResult:
    """Execute `module` over `frame`; return intents, escalations, and the log.

    The module is re-validated first. ``NanoModule`` is a frozen dataclass, so
    in-process code can build one directly and bypass ``from_dict`` — and such a
    module could carry an `intent.emit` node its manifest never granted. "The
    runtime cannot execute invalid IR" has to be enforced here to be true, not
    just asserted in a docstring. The check is one pass over the nodes against an
    evaluation that is nodes × bars.

    Pass ``validate=False`` only in a loop that already validated the same module
    (see ``run_frames``), never to accept a module you did not build.
    """
    checked = module.validate() if validate else module
    return _Machine(module=checked, frame=frame, provider=provider).run()


def run_frames(
    module: NanoModule,
    frames: Sequence[MarketFrame],
    *,
    provider: Optional[ReasoningProvider] = None,
) -> Tuple[ModuleResult, ...]:
    """Execute `module` over several frames in order.

    Validates once rather than once per frame: the module does not change between
    frames, so re-checking it N times would only pay the cost N times.
    """
    checked = module.validate()
    return tuple(
        run_module(checked, frame, provider=provider, validate=False)
        for frame in frames
    )
