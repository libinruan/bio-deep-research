"""Runs one research turn with the Claude Agent SDK and reports progress as events."""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import deque
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

from . import prompts, sources
from .registry import CITATION_RE, Registry
from .tools import SERVER, build_server

ROOT = Path(__file__).resolve().parent.parent
AGENT_HOME = ROOT / "agent_home"  # the agent's working directory; holds .claude/skills
SKILLS_DIR = AGENT_HOME / ".claude" / "skills"

# A third-party Anthropic-compatible endpoint (Moonshot/Kimi, Z.ai/GLM, a local proxy) can be
# used instead of Claude by setting BDR_BASE_URL. Everything the harness does — skills, tools,
# hooks — is client-side and keeps working; only model-specific request fields are dropped.
BASE_URL = os.environ.get("BDR_BASE_URL", "").strip()
THIRD_PARTY = bool(BASE_URL)
MODEL = os.environ.get("BDR_MODEL") or ("" if THIRD_PARTY else "claude-opus-5-5")
EFFORT = {"quick": os.environ.get("BDR_EFFORT_QUICK", "medium"), "deep": os.environ.get("BDR_EFFORT_DEEP", "high")}


def _engine_env() -> dict[str, str]:
    """Environment for the Claude Code subprocess, pointing it at another provider if asked."""
    if not THIRD_PARTY:
        return {}
    # ANTHROPIC_API_KEY must be present and empty, not merely unset: left unset, a Claude
    # Code login on this machine can win and the run quietly bills Claude instead of the
    # provider configured here.
    env = {"ANTHROPIC_BASE_URL": BASE_URL, "ANTHROPIC_API_KEY": ""}
    token = os.environ.get("BDR_AUTH_TOKEN", "").strip()
    if token:
        env["ANTHROPIC_AUTH_TOKEN"] = token
    if MODEL:
        # Through the environment, not the model option, which Claude Code checks against
        # its own model list. Every tier is pinned: background work (session titles and the
        # like) otherwise asks the endpoint for a Claude model it does not serve.
        for var in ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                    "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_FABLE_MODEL"):
            env[var] = MODEL
    # Request features third-party endpoints generally reject. Both variables exist in
    # Claude Code 2.1.288; their effect on these endpoints is not verified here.
    env.setdefault("CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS", "1")
    env.setdefault("ENABLE_TOOL_SEARCH", "false")
    return env


def engine() -> dict[str, Any]:
    return {"model": MODEL or "(endpoint default)", "base_url": BASE_URL or "Anthropic (Claude Code login)",
            "third_party": THIRD_PARTY}

Emit = Callable[[dict[str, Any]], None]


class ResearchError(RuntimeError):
    pass


_NOISE = ("DeprecationWarning", "ExperimentalWarning", "node:internal", "(node:", "punycode")


def _useful(log: deque[str]) -> str:
    """The last few stderr lines that say something, as a quotable detail."""
    lines = [ln for ln in log if not any(n in ln for n in _NOISE)]
    return "\n".join(lines[-6:])[:1200]


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


ERROR_HELP = {
    "authentication_failed": "Claude rejected the login. Run `claude` in a terminal and log in, then try again.",
    "billing_error": "Your Claude account could not be billed for this request (out of credit, or no active plan).",
    "rate_limit": "Your Claude usage limit was reached. Wait for the limit to reset, or try a quick search instead of deep research.",
    "invalid_request": "Claude rejected the request itself. The most common causes are a question the model's safety filters declined to work on, and a conversation that has grown too large to send.",
    "server_error": "The model provider had a server-side error. This is usually temporary; try again.",
    "model_not_found": "The configured model id was not recognised. Check BDR_MODEL against the provider's documentation.",
    "unknown": "Claude returned an unspecified error.",
}


def _assistant_error_text(kind: str, stderr_tail: str) -> str:
    text = f"{ERROR_HELP.get(kind, ERROR_HELP['unknown'])} (reported as `{kind}`)"
    return f"{text}\n\nDetail from Claude Code:\n{stderr_tail}" if stderr_tail else text


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
    log: deque[str] = deque(maxlen=60)  # the CLI writes the real API error here

    def capture(line: str) -> None:
        line = line.strip()
        if line:
            log.append(line)

    options = ClaudeAgentOptions(
        system_prompt=prompts.system_prompt(),
        model=None if THIRD_PARTY else MODEL,
        effort=None if THIRD_PARTY else EFFORT.get(mode, "medium"),
        cwd=str(AGENT_HOME),
        setting_sources=["project"],
        skills=skill_names(),
        tools=["Skill", "Read"],
        allowed_tools=["Skill", "Read", *tool_names],
        permission_mode="dontAsk",
        mcp_servers={SERVER: server},
        hooks={"PreToolUse": [HookMatcher(matcher="Read", hooks=[_confine_reads])]},
        include_partial_messages=True,
        env=_engine_env(),
        stderr=capture,
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
                raise ResearchError(_assistant_error_text(msg.error, _useful(log)))
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


async def _structured(system: str, prompt: str, schema: dict[str, Any], effort: str = "medium") -> dict[str, Any]:
    """One tool-free pass that must come back as JSON matching `schema`."""
    options = ClaudeAgentOptions(
        system_prompt=system, model=None if THIRD_PARTY else MODEL,
        effort=None if THIRD_PARTY else effort,
        cwd=str(AGENT_HOME), setting_sources=[], tools=[], permission_mode="dontAsk",
        env=_engine_env(), max_turns=6,
        output_format=None if THIRD_PARTY else {"type": "json_schema", "schema": schema},
    )
    if THIRD_PARTY:
        prompt += ("\n\nReply with JSON only, no prose and no code fence, matching this schema:\n"
                   + json.dumps(schema))
    final: ResultMessage | None = None
    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, ResultMessage):
            final = msg
    if final is None:
        raise ResearchError("The model returned no result.")
    if final.is_error:
        raise ResearchError(_error_text(final))
    out = final.structured_output
    if out is None and final.result:
        out = _loads(final.result)
    if not isinstance(out, dict):
        raise ResearchError("The model returned no structured result.")
    out["cost_usd"] = final.total_cost_usd
    return out


async def _probe_endpoint() -> str:
    """Ask the configured endpoint directly, to surface the provider's own error.

    The harness retries authentication failures for a long time before giving up, so a wrong
    key would otherwise look like a timeout. One plain request gets the real answer in a second.
    """
    url = BASE_URL.rstrip("/") + "/v1/messages"
    headers = {"content-type": "application/json", "anthropic-version": "2023-06-01"}
    token = os.environ.get("BDR_AUTH_TOKEN", "").strip()
    if token:  # providers differ on which header they read
        headers["x-api-key"] = token
        headers["authorization"] = f"Bearer {token}"
    body = {"model": MODEL, "max_tokens": 16, "messages": [{"role": "user", "content": "Say OK."}]}
    try:
        resp = await sources.client().post(url, json=body, headers=headers, timeout=30.0)
    except Exception as exc:
        return f"Could not reach {url} ({type(exc).__name__}: {exc})."
    if resp.status_code == 200:
        return ""
    detail = resp.text[:300]
    try:
        payload = resp.json()
        detail = (payload.get("error") or {}).get("message") or payload.get("message") or detail
    except ValueError:
        pass
    hint = {
        401: " Check BDR_AUTH_TOKEN, and that the key matches this endpoint and plan.",
        403: " The key was recognised but not allowed to use this endpoint or model.",
        404: " Check BDR_BASE_URL — it should be the provider's Anthropic-compatible address.",
        429: " The provider is rate-limiting or the account is out of credit.",
    }.get(resp.status_code, "")
    return f"{url} returned HTTP {resp.status_code}: {detail}{hint}"


async def check_engine(timeout: float = 60.0) -> dict[str, Any]:
    """One trivial exchange, to prove the configured engine answers before a real search."""
    log: deque[str] = deque(maxlen=60)

    def capture(line: str) -> None:
        line = line.strip()
        if line:
            log.append(line)

    options = ClaudeAgentOptions(
        system_prompt="Reply with the single word OK and nothing else.",
        model=None if THIRD_PARTY else MODEL,
        effort=None if THIRD_PARTY else "low",
        cwd=str(AGENT_HOME), setting_sources=[], tools=[], permission_mode="dontAsk",
        env=_engine_env(), max_turns=1, stderr=capture,
    )
    started = time.monotonic()
    reply, failure = "", ""

    if THIRD_PARTY:
        failure = await _probe_endpoint()
        if failure:
            return {**engine(), "ok": False, "reply": "", "error": failure,
                    "seconds": round(time.monotonic() - started, 1)}

    async def run() -> None:
        nonlocal reply, failure
        async for msg in query(prompt="Say OK.", options=options):
            if isinstance(msg, AssistantMessage):
                if msg.error:
                    failure = _assistant_error_text(msg.error, _useful(log))
                    return
                reply += "".join(getattr(b, "text", "") for b in msg.content
                                 if type(b).__name__ == "TextBlock")
            elif isinstance(msg, ResultMessage) and msg.is_error and not failure:
                failure = _error_text(msg)

    try:
        await asyncio.wait_for(run(), timeout=timeout)
    except asyncio.TimeoutError:
        failure = (f"The endpoint answered a plain request, but the harness got no reply "
                   f"within {timeout:.0f}s. The provider may not support everything Claude "
                   f"Code sends; try another model.")
    except Exception as exc:  # a transport-level failure never reaches the message stream
        failure = f"{type(exc).__name__}: {exc}"

    return {**engine(), "ok": not failure and bool(reply.strip()),
            "reply": reply.strip()[:200], "error": failure,
            "seconds": round(time.monotonic() - started, 1)}


async def compare(run_a: dict[str, Any], run_b: dict[str, Any]) -> dict[str, Any]:
    """Where two runs of the same question agree, differ, and cover different ground."""
    return await _structured(prompts.COMPARE_SYSTEM, prompts.compare_prompt(run_a, run_b),
                             prompts.COMPARE_SCHEMA, effort="high")


def _loads(text: str) -> Any:
    """Parse JSON that may arrive wrapped in prose or a code fence."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ResearchError("The audit did not return JSON.")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise ResearchError(f"The audit returned malformed JSON ({exc}).") from exc


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
    ask = "Audit the citations."
    if THIRD_PARTY:  # no schema enforcement on this endpoint, so state the shape
        ask += (" Reply with JSON only, no prose and no code fence, matching this schema:\n"
                + json.dumps(prompts.AUDIT_SCHEMA))
    prompt = f"<report>\n{report}\n</report>\n\n<sources>\n" + "\n\n".join(blocks) + f"\n</sources>\n\n{ask}"

    options = ClaudeAgentOptions(
        system_prompt=prompts.AUDIT_SYSTEM, model=None if THIRD_PARTY else MODEL, effort=None if THIRD_PARTY else "medium",
        cwd=str(AGENT_HOME), setting_sources=[], tools=[], permission_mode="dontAsk",
        env=_engine_env(), max_turns=6,
        output_format=None if THIRD_PARTY else {"type": "json_schema", "schema": prompts.AUDIT_SCHEMA},
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
        out = _loads(final.result)
    if not isinstance(out, dict):
        raise ResearchError("The audit returned no structured result.")
    out["cost_usd"] = final.total_cost_usd
    return out
