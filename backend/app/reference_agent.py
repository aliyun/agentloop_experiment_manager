"""Deterministic reference Agent for validating the packaged connector path."""

from __future__ import annotations

import ast
import operator
import re
from typing import Any
from uuid import uuid4

from app.agent_connector import LocalAgentResponse

_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


async def handle(payload: dict[str, Any]) -> LocalAgentResponse:
    value = payload.get("input")
    if not isinstance(value, str):
        raise ValueError("payload.input must be a string")
    question = value.split("<experiment_context", 1)[0].strip()
    match = re.search(r"Compute\s+(.+?)(?:\.\s*Return|$)", question, flags=re.IGNORECASE)
    if not match:
        raise ValueError("reference Agent only accepts 'Compute <expression>' tasks")
    answer = evaluate_expression(match.group(1).strip())
    if isinstance(answer, float) and answer.is_integer():
        answer = int(answer)
    return LocalAgentResponse(
        output=str(answer),
        meta={
            "traceId": uuid4().hex,
            "agentName": "reference-calculator-agent",
            "agentVersion": "1.0.0",
        },
    )


def evaluate_expression(expression: str) -> int | float:
    tree = ast.parse(expression, mode="eval")

    def visit(node: ast.AST) -> int | float:
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _OPERATORS:
            return _OPERATORS[type(node.op)](visit(node.left), visit(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _OPERATORS:
            return _OPERATORS[type(node.op)](visit(node.operand))
        raise ValueError("expression contains an unsupported operation")

    result = visit(tree)
    if abs(float(result)) > 1e12:
        raise ValueError("expression result exceeds the safety limit")
    return result
