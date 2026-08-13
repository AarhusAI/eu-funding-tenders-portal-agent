"""Scripted tool-call sequences for the mock LLM.

We don't run a real model — we inspect the conversation history and the latest
user message, then decide what the model should say next. The flow is keyed on a
few substring matches that exercise the agent's tool paths:

  mentions a topic identifier ("HORIZON-CL5-...")  -> get_topic -> answer
  mentions "closed" / "afsluttede"                 -> search_topics(statuses=["Closed"]) -> answer
  mentions "cluster 5" / "cl5"                     -> search_topics(clusters=["CL5"]) -> answer
  default                                          -> search_topics(text=query) -> answer

A real model would also vary the reply language; here we answer in Danish only
when the question obviously is Danish, which is enough to show the hint working.
"""

from __future__ import annotations

import json
import re
from typing import Any

# Matches a call-topic code such as HORIZON-CL5-2027-01-D1-10.
IDENTIFIER_RE = re.compile(r"\b(HORIZON|DIGITAL)-[A-Z0-9-]{6,}\b", re.IGNORECASE)

DANISH_MARKERS = ("hvilke", "hvad", "kald", "ansøg", "frist", "muligheder", "projekter")


def _latest_user_message(messages: list[dict[str, Any]]) -> str:
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def _tool_calls_so_far(messages: list[dict[str, Any]]) -> list[str]:
    """Names of tools the assistant has already requested, in order."""
    called: list[str] = []
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        for call in msg.get("tool_calls") or []:
            name = (call.get("function") or {}).get("name")
            if name:
                called.append(name)
    return called


def _tool_call(name: str, arguments: dict[str, Any], idx: int) -> dict[str, Any]:
    return {
        "id": f"call_{name}_{idx}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def _final(content: str) -> dict[str, Any]:
    return {"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}


def _tool(calls: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "message": {"role": "assistant", "content": None, "tool_calls": calls},
        "finish_reason": "tool_calls",
    }


def respond(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Decide what the assistant should say next given the conversation so far."""
    query = _latest_user_message(messages)
    # Strip the "[language=xx] " prefix the agent prepends before matching.
    plain = re.sub(r"^\[language=[a-z]{2}\]\s*", "", query)
    lowered = plain.lower()
    step = len(_tool_calls_so_far(messages))
    danish = "[language=da]" in query or any(m in lowered for m in DANISH_MARKERS)

    identifier = IDENTIFIER_RE.search(plain)
    if identifier:
        if step == 0:
            return _tool([_tool_call("get_topic", {"identifier": identifier.group(0)}, step)])
        return _final(
            f"{identifier.group(0)} er hentet i fuld længde — se emnebeskrivelsen, "
            "status og frist ovenfor."
            if danish
            else f"Here are the full details for {identifier.group(0)}, including its "
            "status, deadline and description."
        )

    if step == 0:
        if "closed" in lowered or "afslutt" in lowered:
            # Exercise the escape hatch: a status the default profile excludes.
            return _tool([_tool_call("search_topics", {"statuses": ["Closed"]}, step)])
        if "cluster 5" in lowered or "cl5" in lowered:
            return _tool(
                [_tool_call("search_topics", {"clusters": ["CL5"], "topic_contains": []}, step)]
            )
        return _tool([_tool_call("search_topics", {"text": plain}, step)])

    return _final(
        "Her er de emner der matcher din søgeprofil, rangeret efter hvor mange "
        "nøgleord de rammer. Hvert resultat viser sin score og de matchede nøgleord, "
        "samt emnekoden du kan slå op på portalen."
        if danish
        else "Here are the call topics matching your search profile, ranked by how many "
        "of its keywords they hit. Each result shows its score, the matched keywords "
        "and the topic identifier you can look up on the portal."
    )
