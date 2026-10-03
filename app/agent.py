"""Runs one research turn with the Claude Agent SDK and reports progress as events."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookMatcher,
    ResultMessage,
    StreamEvent,
    ToolUseBlock,
    query,
)

from . import prompts
from .registry import CITATION_RE, Registry
from .tools import SERVER, build_server

ROOT = Path(__file__).resolve().parent.parent
AGENT_HOME = ROOT / "agent_home"  # the agent's working directory; holds .claude/skills
SKILLS_DIR = AGENT_HOME / ".claude" / "skills"

MODEL = os.environ.get("BDR_MODEL", "claude-opus-5-5")
EFFORT = {"quick": os.environ.get("BDR_EFFORT_QUICK", "medium"), "deep": os.environ.get("BDR_EFFORT_DEEP", "high")}

Emit = Callable[[dict[str, Any]], None]


class ResearchError(RuntimeError):
    pass


def skill_names() -> list[str]:
    return sorted(p.parent.name for p in SKILLS_DIR.glob("*/SKILL.md"))


async def _confine_reads(hook_input: Any, tool_use_id: str | None, context: Any) -> dict[str, Any]:
    """The agent may read skill files and nothing else on this machine."""
    raw = (hook_input.get("tool_input") or {}).get("file_path") or ""
    try:
        target = (AGENT_HOME / raw).resolve()
        allowed = target.is_relative_to(SKILLS_DIR.resolve())
    except (OSError, ValueError):
        allowed = False
    if allowed:
        return {}
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": "Only files inside the skills directory can be read.",
    }}


def _error_text(msg: ResultMessage) -> str:
    detail = "; ".join(msg.errors or []) or msg.result or msg.subtype
    if msg.subtype == "error_max_turns":
        return "The agent hit its step limit before finishing. Try a narrower question."
    if msg.stop_reason == "refusal":
        return "The model declined this request (safety filter). Rephrase the question, or avoid operational detail on hazardous agents."
    return f"The research run failed: {detail}"


async def research(
    registry: Registry, emit: Emit, question: str, *, mode: str = "quick", hypotheses: bool = False,
    session_id: str | None = None, year_from: int | None = None, year_to: int | None = None,
) -> dict[str, Any]:
    """Run the agent on one question. Returns {report, session_id, usage}; the report still has raw identifier citations."""
    server, tool_names = build_server(registry, emit)
    options = ClaudeAgentOptions(
        system_prompt=prompts.system_prompt(),
        model=MODEL,
        effort=EFFORT.get(mode, "medium"),
        cwd=str(AGENT_HOME),
        setting_sources=["project"],
        skills=skill_names(),
        tools=["Skill", "Read"],
        allowed_tools=["Skill", "Read", *tool_names],
        permission_mode="dontAsk",
        mcp_servers={SERVER: server},
        hooks={"PreToolUse": [HookMatcher(matcher="Read", hooks=[_confine_reads])]},
        include_partial_messages=True,
        resume=session_id,
        max_turns=120 if mode == "deep" else 40,
    )
    prompt = prompts.user_prompt(question, mode, hypotheses, follow_up=session_id is not None,
                                 year_from=year_from, year_to=year_to)

    final: ResultMessage | None = None
    last_text = ""
    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, StreamEvent):
            if msg.parent_tool_use_id:
                continue
            event = msg.event
            if event.get("type") == "message_start":
                emit({"type": "text_reset"})
            elif event.get("type") == "content_block_delta" and event["delta"].get("type") == "text_delta":
                emit({"type": "delta", "text": event["delta"]["text"]})
        elif isinstance(msg, AssistantMessage):
            if msg.error:
                raise ResearchError(f"Claude returned an error ({msg.error}). If this is an authentication error, run `claude` and log in.")
            text = "".join(getattr(b, "text", "") for b in msg.content if type(b).__name__ == "TextBlock")
            if text.strip():
                last_text = text
            for block in msg.content:
                # The app's own tools announce themselves; built-in tools are announced here.
                if isinstance(block, ToolUseBlock) and not block.name.startswith("mcp__"):
                    detail = block.input.get("skill") or Path(str(block.input.get("file_path", ""))).name
                    emit({"type": "tool", "name": block.name, "input": {"target": detail}})
        elif isinstance(msg, ResultMessage):
            final = msg

    if final is None:
        raise ResearchError("The agent ended without returning a result.")
    if final.is_error:
        raise ResearchError(_error_text(final))
    report = (final.result or last_text).strip()
    # Drop a short lead-in ("Here is the report…") before the report's title.
    head, sep, rest = report.partition("\n# ")
    if sep and len(head) < 400 and not head.startswith("#"):
        report = "# " + rest
    return {
        "report": report,
        "session_id": final.session_id,
        "usage": {"cost_usd": final.total_cost_usd, "turns": final.num_turns, "seconds": round(final.duration_ms / 1000)},
    }


async def audit(registry: Registry, report: str) -> dict[str, Any]:
    """Second pass: an independent check of each citation against the record it points to."""
    cited: dict[str, dict[str, Any]] = {}
    for m in CITATION_RE.finditer(report):
        for token in m.group(1).replace(",", ";").split(";"):
            rec = registry.get(token)
            if rec:
                cited[rec["key"]] = rec
    if not cited:
        return {"citations_checked": 0, "issues": []}

    blocks = []
    for key, rec in cited.items():
        flags = ", ".join(f for f in (
            rec.get("design"), "PREPRINT" if rec.get("preprint") else "", "RETRACTED" if rec.get("retracted") else "",
            "/".join(rec.get("species") or []), "full text was read" if rec.get("full_text_read") else "abstract only",
        ) if f)
        blocks.append(f'<source id="{key}">\nTitle: {rec.get("title")}\nFlags: {flags}\n{rec.get("abstract") or "(no abstract)"}\n</source>')
    prompt = f"<report>\n{report}\n</report>\n\n<sources>\n" + "\n\n".join(blocks) + "\n</sources>\n\nAudit the citations."

    options = ClaudeAgentOptions(
        system_prompt=prompts.AUDIT_SYSTEM, model=MODEL, effort="medium",
        cwd=str(AGENT_HOME), setting_sources=[], tools=[], permission_mode="dontAsk",
        output_format={"type": "json_schema", "schema": prompts.AUDIT_SCHEMA}, max_turns=6,
    )
    final: ResultMessage | None = None
    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, ResultMessage):
            final = msg
    if final is None:
        raise ResearchError("The audit ended without returning a result.")
    if final.is_error:
        raise ResearchError(_error_text(final))
    out = final.structured_output
    if out is None and final.result:
        out = json.loads(final.result)
    if not isinstance(out, dict):
        raise ResearchError("The audit returned no structured result.")
    out["cost_usd"] = final.total_cost_usd
    return out
