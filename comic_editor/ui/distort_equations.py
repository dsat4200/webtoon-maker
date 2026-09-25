"""Small, deliberately non-executable expression language for image remapping."""
from __future__ import annotations

import ast
from functools import lru_cache
import operator

import numpy as np


_FUNCTIONS = {
    "sin": np.sin, "cos": np.cos, "tan": np.tan, "asin": np.arcsin,
    "acos": np.arccos, "atan": np.arctan, "atan2": np.arctan2,
    "sinh": np.sinh, "cosh": np.cosh, "tanh": np.tanh,
    "sqrt": np.sqrt, "abs": np.abs, "floor": np.floor, "ceil": np.ceil,
    "round": np.round, "exp": np.exp, "log": np.log, "log10": np.log10,
    "min": np.minimum, "max": np.maximum, "pow": np.power,
    "mod": np.mod, "sign": np.sign, "hypot": np.hypot,
    "clamp": np.clip, "lerp": lambda a, b, t: a + (b - a) * t,
    "if": np.where, "where": np.where,
}
_BINARY = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
           ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
           ast.Mod: operator.mod, ast.Pow: np.power}
_COMPARE = {ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt,
            ast.GtE: operator.ge, ast.Eq: operator.eq, ast.NotEq: operator.ne}
_VARIABLES = {"x", "y", "w", "h", "r", "t", "a", "b", "c", "pi", "e",
              "rx", "ry", "u", "v"}


@lru_cache(maxsize=128)
def _parse(expression: str):
    if not isinstance(expression, str) or len(expression) > 2048:
        raise ValueError("Equations must contain at most 2048 characters")
    try:
        tree = ast.parse(expression.replace("^", "**"), mode="eval")
    except (SyntaxError, RecursionError) as error:
        raise ValueError("Invalid distortion equation") from error
    if sum(1 for _ in ast.walk(tree)) > 256:
        raise ValueError("Distortion equation is too complex")

    def validate(node, depth=0):
        if depth > 32:
            raise ValueError("Distortion equation is too deeply nested")
        if isinstance(node, ast.Expression):
            validate(node.body, depth + 1)
        elif isinstance(node, ast.Constant):
            if type(node.value) not in (int, float) or abs(node.value) > 1e12:
                raise ValueError("Equation constants must be finite small numbers")
        elif isinstance(node, ast.Name):
            if node.id not in _VARIABLES:
                raise ValueError(f"Unknown equation variable: {node.id}")
        elif isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
            validate(node.left, depth + 1)
            validate(node.right, depth + 1)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub, ast.Not)):
            validate(node.operand, depth + 1)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCTIONS:
            if node.keywords or not 1 <= len(node.args) <= 3:
                raise ValueError("Equation functions accept one to three positional arguments")
            for argument in node.args:
                validate(argument, depth + 1)
        elif isinstance(node, ast.Compare) and all(type(op) in _COMPARE for op in node.ops):
            for child in [node.left, *node.comparators]:
                validate(child, depth + 1)
        elif isinstance(node, ast.IfExp):
            for child in (node.test, node.body, node.orelse):
                validate(child, depth + 1)
        elif isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
            for child in node.values:
                validate(child, depth + 1)
        else:
            raise ValueError("Only numbers, coordinates, arithmetic, and approved math functions are allowed")

    validate(tree)
    return tree.body


def validate_equation(expression: str) -> None:
    """Validate without evaluating; this can also back a controls error message."""
    _parse(expression)


def evaluate_equation(expression: str, variables: dict):
    """Interpret approved AST nodes. Python eval/compile are never involved."""
    def visit(node):
        if isinstance(node, ast.Constant):
            return float(node.value)
        if isinstance(node, ast.Name):
            return variables.get(node.id, {"pi": np.pi, "e": np.e}.get(node.id, 0.))
        if isinstance(node, ast.BinOp):
            lhs, rhs = visit(node.left), visit(node.right)
            # A bounded exponent prevents malicious huge scalar integer powers.
            if isinstance(node.op, ast.Pow):
                rhs = np.clip(rhs, -128., 128.)
            return _BINARY[type(node.op)](lhs, rhs)
        if isinstance(node, ast.UnaryOp):
            value = visit(node.operand)
            return np.logical_not(value) if isinstance(node.op, ast.Not) else (-value if isinstance(node.op, ast.USub) else value)
        if isinstance(node, ast.Call):
            try:
                return _FUNCTIONS[node.func.id](*(visit(arg) for arg in node.args))
            except TypeError as error:
                raise ValueError(f"Invalid arguments for {node.func.id}") from error
        if isinstance(node, ast.Compare):
            left = visit(node.left)
            result = True
            for op, right_node in zip(node.ops, node.comparators):
                right = visit(right_node)
                result = np.logical_and(result, _COMPARE[type(op)](left, right))
                left = right
            return result
        if isinstance(node, ast.IfExp):
            return np.where(visit(node.test), visit(node.body), visit(node.orelse))
        if isinstance(node, ast.BoolOp):
            result = visit(node.values[0])
            function = np.logical_and if isinstance(node.op, ast.And) else np.logical_or
            for value in node.values[1:]:
                result = function(result, visit(value))
            return result
        raise ValueError("Invalid equation")

    with np.errstate(all="ignore"):
        return visit(_parse(expression))
