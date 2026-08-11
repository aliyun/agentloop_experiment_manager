#!/usr/bin/env python3
"""A real deterministic local Agent used for end-to-end launcher acceptance.

This is intentionally small, but it uses the same Agent-initiated WebSocket
protocol as customer Agents.  Replace ``handle`` with the customer's Agent
call while keeping ``AgentConnector`` unchanged.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import operator
import os
import re
from typing import Any
from uuid import uuid4

from app.agent_connector import AgentConnector, LocalAgentResponse

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
    answer = _evaluate_expression(match.group(1).strip())
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


def _evaluate_expression(expression: str) -> int | float:
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="connectUrl returned by the launcher")
    parser.add_argument(
        "--token-env",
        default="AGENT_CONNECTION_TOKEN",
        help="environment variable containing the one-time Agent connection token",
    )
    parser.add_argument("--no-reconnect", action="store_true")
    args = parser.parse_args()
    token = os.environ.get(args.token_env, "")
    if not token:
        raise SystemExit(f"Set {args.token_env} to the Agent connection token")
    asyncio.run(
        AgentConnector(
            connect_url=args.url,
            token=token,
            handler=handle,
            reconnect=not args.no_reconnect,
        ).run()
    )


if __name__ == "__main__":
    main()
