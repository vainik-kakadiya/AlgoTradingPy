"""Declarative JSON rule engine.

Rules are validated Pydantic trees - never Python code - and are evaluated by walking
the tree. ``eval``/``exec`` are not used anywhere.

Example::

    {"op": "and", "conditions": [
        {"op": "gt", "left": {"type": "field", "name": "range"}, "right": 100},
        {"op": "crosses_above",
         "left": {"type": "indicator", "name": "ema", "period": 9},
         "right": {"type": "indicator", "name": "ema", "period": 21}}
    ]}

Evaluation uses three-valued logic: a comparison whose operands cannot be computed yet
(insufficient history, missing volume) yields ``None`` ("unknown"), and an unknown result
never produces a signal - including under ``not``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from app.strategies import indicators
from app.trading.types import Candle

MAX_DEPTH = 12
MAX_NODES = 200
MAX_LOOKBACK = 500
FLOAT_TOLERANCE = 1e-9

FieldName = Literal["open", "high", "low", "close", "volume", "range", "body", "abs_body", "change", "change_pct"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ConstOperand(_Strict):
    type: Literal["const"] = "const"
    value: float


class FieldOperand(_Strict):
    type: Literal["field"] = "field"
    name: FieldName
    offset: int = Field(0, ge=0, le=MAX_LOOKBACK, description="Bars back from the latest completed candle")


class IndicatorOperand(_Strict):
    type: Literal["indicator"] = "indicator"
    name: Literal["sma", "ema", "rsi", "vwap"]
    period: int | None = Field(None, ge=1, le=MAX_LOOKBACK)
    source: Literal["open", "high", "low", "close", "hl2", "hlc3"] = "close"
    offset: int = Field(0, ge=0, le=MAX_LOOKBACK)

    @model_validator(mode="after")
    def _period_required(self) -> IndicatorOperand:
        if self.name != "vwap" and self.period is None:
            raise ValueError(f"Indicator '{self.name}' requires a period")
        return self


def _coerce_operand(value: Any) -> Any:
    if isinstance(value, bool):
        raise ValueError("Booleans are not valid operands")
    if isinstance(value, (int, float)):
        return {"type": "const", "value": value}
    return value


Operand = Annotated[
    Union[ConstOperand, FieldOperand, IndicatorOperand],
    BeforeValidator(_coerce_operand),
    Field(discriminator="type"),
]


class Comparison(_Strict):
    op: Literal["gt", "lt", "gte", "lte", "eq", "neq", "crosses_above", "crosses_below"]
    left: Operand
    right: Operand


class AndNode(_Strict):
    op: Literal["and"]
    conditions: list[Condition] = Field(min_length=1)


class OrNode(_Strict):
    op: Literal["or"]
    conditions: list[Condition] = Field(min_length=1)


class NotNode(_Strict):
    op: Literal["not"]
    condition: Condition


Condition = Annotated[Union[Comparison, AndNode, OrNode, NotNode], Field(discriminator="op")]

AndNode.model_rebuild()
OrNode.model_rebuild()
NotNode.model_rebuild()


class RuleValidationError(ValueError):
    pass


def check_complexity(node: Any, depth: int = 1) -> int:
    """Return node count; raise if the tree is too deep or too large."""
    if depth > MAX_DEPTH:
        raise RuleValidationError(f"Rule nesting exceeds maximum depth of {MAX_DEPTH}")
    if isinstance(node, (AndNode, OrNode)):
        count = 1 + sum(check_complexity(c, depth + 1) for c in node.conditions)
    elif isinstance(node, NotNode):
        count = 1 + check_complexity(node.condition, depth + 1)
    else:
        count = 1
    if count > MAX_NODES:
        raise RuleValidationError(f"Rule has more than {MAX_NODES} nodes")
    return count


def required_lookback(node: Any) -> int:
    """Number of completed candles needed before the rule can be fully evaluated."""
    if isinstance(node, (AndNode, OrNode)):
        return max(required_lookback(c) for c in node.conditions)
    if isinstance(node, NotNode):
        return required_lookback(node.condition)
    extra = 1 if node.op in ("crosses_above", "crosses_below") else 0
    return max(_operand_lookback(node.left), _operand_lookback(node.right)) + extra


def _operand_lookback(op: Any) -> int:
    if isinstance(op, FieldOperand):
        return op.offset + (2 if op.name in ("change", "change_pct") else 1)
    if isinstance(op, IndicatorOperand):
        period = op.period or 1
        return op.offset + (period + 1 if op.name == "rsi" else period)
    return 0


# ---------------------------------------------------------------------------- evaluation


def _field_value(candles: Sequence[Candle], name: str, offset: int) -> float | None:
    idx = len(candles) - 1 - offset
    if idx < 0:
        return None
    c = candles[idx]
    if name == "range":
        return c.range
    if name == "body":
        return c.body
    if name == "abs_body":
        return c.abs_body
    if name in ("change", "change_pct"):
        if idx < 1:
            return None
        prev = candles[idx - 1].close
        diff = c.close - prev
        return diff if name == "change" else (diff / prev * 100 if prev else None)
    return getattr(c, name)


def operand_value(op: Any, candles: Sequence[Candle], extra_offset: int = 0) -> float | None:
    if isinstance(op, ConstOperand):
        return op.value
    if isinstance(op, FieldOperand):
        return _field_value(candles, op.name, op.offset + extra_offset)
    if isinstance(op, IndicatorOperand):
        cut = op.offset + extra_offset
        window = candles[: len(candles) - cut] if cut else candles
        if not window:
            return None
        return indicators.compute(op.name, window, op.period, op.source)
    raise RuleValidationError(f"Unsupported operand {op!r}")


def describe_operand(op: Any) -> str:
    if isinstance(op, ConstOperand):
        return f"{op.value:g}"
    if isinstance(op, FieldOperand):
        return op.name + (f"[-{op.offset}]" if op.offset else "")
    if isinstance(op, IndicatorOperand):
        args = f"{op.period}" if op.period else ""
        if op.source != "close" and op.name != "vwap":
            args += f", {op.source}"
        return f"{op.name.upper()}({args})" + (f"[-{op.offset}]" if op.offset else "")
    return "?"


_SYMBOLS = {"gt": ">", "lt": "<", "gte": ">=", "lte": "<=", "eq": "==", "neq": "!=", "crosses_above": "crosses above", "crosses_below": "crosses below"}


def describe(node: Any) -> str:
    if isinstance(node, AndNode):
        return "(" + " AND ".join(describe(c) for c in node.conditions) + ")"
    if isinstance(node, OrNode):
        return "(" + " OR ".join(describe(c) for c in node.conditions) + ")"
    if isinstance(node, NotNode):
        return f"NOT {describe(node.condition)}"
    return f"{describe_operand(node.left)} {_SYMBOLS[node.op]} {describe_operand(node.right)}"


def _compare(op: str, a: float, b: float) -> bool:
    if op == "gt":
        return a > b
    if op == "lt":
        return a < b
    if op == "gte":
        return a >= b
    if op == "lte":
        return a <= b
    if op == "eq":
        return abs(a - b) <= FLOAT_TOLERANCE
    if op == "neq":
        return abs(a - b) > FLOAT_TOLERANCE
    raise RuleValidationError(f"Unsupported operator {op}")


def evaluate(node: Any, candles: Sequence[Candle], trace: list[dict[str, Any]] | None = None) -> bool | None:
    """Evaluate a condition against completed candles (latest last). Returns True/False/None."""
    if isinstance(node, AndNode):
        results = [evaluate(c, candles, trace) for c in node.conditions]
        if any(r is False for r in results):
            return False
        return None if any(r is None for r in results) else True
    if isinstance(node, OrNode):
        results = [evaluate(c, candles, trace) for c in node.conditions]
        if any(r is True for r in results):
            return True
        return None if any(r is None for r in results) else False
    if isinstance(node, NotNode):
        inner = evaluate(node.condition, candles, trace)
        return None if inner is None else not inner

    left = operand_value(node.left, candles)
    right = operand_value(node.right, candles)
    entry: dict[str, Any] = {"rule": describe(node), "left": left, "right": right}
    if left is None or right is None:
        result: bool | None = None
    elif node.op in ("crosses_above", "crosses_below"):
        prev_left = operand_value(node.left, candles, 1)
        prev_right = operand_value(node.right, candles, 1)
        entry.update(prev_left=prev_left, prev_right=prev_right)
        if prev_left is None or prev_right is None:
            result = None
        elif node.op == "crosses_above":
            result = left > right and prev_left <= prev_right
        else:
            result = left < right and prev_left >= prev_right
    else:
        result = _compare(node.op, left, right)
    entry["result"] = result
    if trace is not None:
        trace.append(entry)
    return result
