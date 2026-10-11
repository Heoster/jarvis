"""Safe arithmetic — AST-validated, never `eval`.

Small models are unreliable at mental maths, so giving them a calculator tool is
one of the highest-value things you can do for accuracy.  This one evaluates
expressions through a whitelist of AST nodes and works in exact ``Decimal`` when
asked, so money maths does not drift.
"""

from __future__ import annotations

import ast
import math
import operator
from decimal import Decimal, getcontext
from typing import Any, Callable, Dict, Union

from ...utils.logger import get_logger
from ..base import tool

log = get_logger("utility.calc")

__all__ = ["calculate", "CALCULATOR_TOOLS"]

getcontext().prec = 28

Number = Union[int, float, Decimal]

_ALLOWED_BINOPS: Dict[type, Callable[[Any, Any], Any]] = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod, ast.Pow: operator.pow,
}
_ALLOWED_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}

_FUNCTIONS: Dict[str, Callable[..., Any]] = {
    "abs": abs, "round": round, "min": min, "max": max, "sum": sum,
    "sqrt": math.sqrt, "log": math.log, "log10": math.log10, "log2": math.log2,
    "exp": math.exp, "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "asin": math.asin, "acos": math.acos, "atan": math.atan, "atan2": math.atan2,
    "degrees": math.degrees, "radians": math.radians, "floor": math.floor,
    "ceil": math.ceil, "factorial": math.factorial, "gcd": math.gcd,
    "hypot": math.hypot, "pow": pow, "int": int, "float": float,
    "pi": math.pi, "e": math.e, "tau": math.tau, "inf": math.inf,
}

_CONSTANTS = {"pi": math.pi, "e": math.e, "tau": math.tau, "inf": math.inf,
              "True": True, "False": False}


class CalculatorError(ValueError):
    pass


def _eval(node: ast.AST, *, decimal: bool = False) -> Any:
    if isinstance(node, ast.Expression):
        return _eval(node.body, decimal=decimal)
    if isinstance(node, ast.Constant):
        value = node.value
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return Decimal(str(value)) if decimal else value
        if isinstance(value, str):
            return value
        raise CalculatorError(f"unsupported constant: {value!r}")
    if isinstance(node, ast.BinOp):
        op_type = type(node.op)
        if op_type not in _ALLOWED_BINOPS:
            raise CalculatorError(f"operator {op_type.__name__} not allowed")
        left = _eval(node.left, decimal=decimal)
        right = _eval(node.right, decimal=decimal)
        if isinstance(right, (int, float)) and right == 0 and op_type in (
                ast.Div, ast.FloorDiv, ast.Mod):
            raise CalculatorError("division by zero")
        return _ALLOWED_BINOPS[op_type](left, right)
    if isinstance(node, ast.UnaryOp):
        operand = _eval(node.operand, decimal=decimal)
        return _ALLOWED_UNARY[type(node.op)](operand)
    if isinstance(node, ast.Name):
        if node.id in _CONSTANTS:
            return _CONSTANTS[node.id]
        raise CalculatorError(f"unknown name '{node.id}'")
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise CalculatorError("only simple function calls are allowed")
        name = node.func.id
        if name not in _FUNCTIONS:
            raise CalculatorError(f"unknown function '{name}'")
        args = [_eval(arg, decimal=decimal) for arg in node.args]
        if decimal:
            args = [float(a) if isinstance(a, Decimal) else a for a in args]
        return _FUNCTIONS[name](*args)
    if isinstance(node, ast.Compare):
        left = _eval(node.left, decimal=decimal)
        for op, comparator in zip(node.ops, node.comparators):
            right = _eval(comparator, decimal=decimal)
            ok = {ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt,
                  ast.GtE: operator.ge, ast.Eq: operator.eq,
                  ast.NotEq: operator.ne}.get(type(op))
            if ok is None or not ok(left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.BoolOp):
        values = [_eval(value, decimal=decimal) for value in node.values]
        return all(values) if isinstance(node.op, ast.And) else any(values)
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_eval(item, decimal=decimal) for item in node.elts]
    raise CalculatorError(f"unsupported expression: {type(node).__name__}")


def evaluate(expression: str, *, decimal: bool = False) -> Any:
    """Evaluate a maths expression safely."""
    cleaned = (expression or "").strip()
    if not cleaned:
        raise CalculatorError("empty expression")
    cleaned = cleaned.replace("^", "**").replace("×", "*").replace("÷", "/")
    cleaned = cleaned.replace("−", "-")
    if len(cleaned) > 500:
        raise CalculatorError("expression too long")
    try:
        tree = ast.parse(cleaned, mode="eval")
    except SyntaxError as exc:
        raise CalculatorError(f"cannot parse '{expression}': {exc.msg}") from exc
    result = _eval(tree, decimal=decimal)
    if isinstance(result, float) and (math.isnan(result) or math.isinf(result)):
        return result
    return result


def format_number(value: Any) -> str:
    if isinstance(value, Decimal):
        normalised = value.normalize()
        text = format(normalised, "f")
        return text
    if isinstance(value, float):
        if value.is_integer() and abs(value) < 1e15:
            return f"{int(value):,}"
        return f"{value:,.10g}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


@tool(category="utility", timeout=15)
def calculate(expression: str, exact: bool = False) -> str:
    """Evaluate a maths expression precisely. ALWAYS use this instead of doing
    arithmetic in your head.

    Supports + - * / // % ** parentheses, and functions like sqrt, log, sin,
    cos, abs, round, min, max, factorial. Use ^ or ** for powers.

    Args:
        expression: e.g. "(1250 * 0.18) + 99", "sqrt(2)", "18% of 4500" → "0.18*4500"
        exact: Use Decimal maths (best for money/INR amounts)
    """
    try:
        value = evaluate(expression, decimal=exact)
    except CalculatorError as exc:
        return (False, f"Cannot calculate '{expression}': {exc}")
    except Exception as exc:
        return f"Calculation failed: {exc}"
    return f"{expression} = **{format_number(value)}**"


@tool(category="utility", timeout=15)
def percentage(part: float, whole: float) -> str:
    """What percentage is `part` of `whole`? (also returns the ratio)

    Args:
        part: The portion
        whole: The total
    """
    if whole == 0:
        return "Cannot divide by zero (whole = 0)."
    pct = part / whole * 100
    return (f"{format_number(part)} is {pct:.4g}% of {format_number(whole)} "
            f"(ratio {part / whole:.6g})")


CALCULATOR_TOOLS = [calculate, percentage]
