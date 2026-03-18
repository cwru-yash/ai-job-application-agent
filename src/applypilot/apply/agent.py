"""Apply agent backends used by the auto-apply launcher."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from applypilot.apply.chrome import _kill_process_tree

ActionCallback = Callable[[str], None]
_BACKENDS = ("claude", "command")
_DEFAULT_TIMEOUT = 300

_agent_procs: dict[int, subprocess.Popen] = {}
_agent_lock = threading.Lock()


@dataclass(slots=True)
class AgentRunResult:
    """Normalized output from an apply agent backend."""

    output: str
    duration_ms: int
    stats: dict[str, Any] = field(default_factory=dict)
    skipped: bool = False


class ApplyAgent:
    """Base class for apply agent backends."""

    backend = "unknown"

    def run(
        self,
        *,
        prompt: str,
        model: str,
        worker_id: int,
        port: int,
        worker_dir: Path,
        mcp_config_path: Path,
        worker_log: Path,
        on_action: ActionCallback | None = None,
    ) -> AgentRunResult:
        raise NotImplementedError


def get_apply_backend_name(backend: str | None = None) -> str:
    """Resolve the configured apply backend name."""
    resolved = (backend or os.environ.get("APPLYPILOT_APPLY_BACKEND") or "claude").strip().lower()
    if resolved not in _BACKENDS:
        valid = ", ".join(_BACKENDS)
        raise ValueError(f"Unknown apply backend '{resolved}'. Choose from: {valid}")
    return resolved


def get_command_agent_command(command: str | None = None) -> str | None:
    """Resolve the configured command backend command."""
    resolved = (command or os.environ.get("APPLYPILOT_AGENT_COMMAND") or "").strip()
    return resolved or None


def render_command_agent_command(
    command: str | None,
    *,
    model: str,
    mcp_config_path: Path,
    worker_dir: Path,
    port: int,
    worker_id: int,
) -> str:
    """Render a command backend command string with placeholders expanded."""
    resolved = get_command_agent_command(command)
    if not resolved:
        raise ValueError("The command apply backend requires a command string.")

    placeholders = {
        "model": model,
        "mcp_config": str(mcp_config_path),
        "worker_dir": str(worker_dir),
        "port": str(port),
        "worker_id": str(worker_id),
    }
    try:
        return resolved.format_map(placeholders)
    except KeyError as exc:
        name = exc.args[0]
        raise ValueError(f"Unknown placeholder '{{{name}}}' in apply agent command.") from exc


def get_apply_agent_timeout() -> int:
    """Resolve the backend process timeout in seconds."""
    raw = (os.environ.get("APPLYPILOT_AGENT_TIMEOUT") or "").strip()
    if not raw:
        return _DEFAULT_TIMEOUT
    try:
        return max(30, int(raw))
    except ValueError:
        return _DEFAULT_TIMEOUT


def kill_active_agents() -> None:
    """Kill all active agent processes."""
    with _agent_lock:
        procs = list(_agent_procs.values())
        _agent_procs.clear()
    for proc in procs:
        if proc.poll() is None:
            _kill_process_tree(proc.pid)


def build_apply_agent(backend: str | None = None, command: str | None = None) -> ApplyAgent:
    """Build the configured apply agent backend."""
    resolved = get_apply_backend_name(backend)
    timeout = get_apply_agent_timeout()
    if resolved == "claude":
        return ClaudeCodeAgent(timeout=timeout)

    resolved_command = get_command_agent_command(command)
    if not resolved_command:
        raise ValueError(
            "The command apply backend requires --agent-command or APPLYPILOT_AGENT_COMMAND."
        )
    return CommandApplyAgent(command=resolved_command, timeout=timeout)


def _tool_desc(name: str, tool_input: dict[str, Any]) -> str:
    if "url" in tool_input:
        return f"{name} {tool_input['url'][:60]}"
    if "ref" in tool_input:
        return f"{name} {tool_input.get('element', tool_input.get('text', ''))}"[:50]
    if "fields" in tool_input:
        return f"{name} ({len(tool_input['fields'])} fields)"
    if "paths" in tool_input:
        return f"{name} upload"
    return name


def _handle_structured_line(
    line: str,
    text_parts: list[str],
    log_file,
    on_action: ActionCallback | None,
    stats: dict[str, Any],
) -> bool:
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        return False

    if not isinstance(msg, dict):
        return False

    msg_type = msg.get("type")
    if msg_type == "assistant":
        for block in msg.get("message", {}).get("content", []):
            block_type = block.get("type")
            if block_type == "text":
                text = block.get("text", "")
                if text:
                    text_parts.append(text)
                    log_file.write(text + "\n")
            elif block_type == "tool_use":
                name = (
                    block.get("name", "")
                    .replace("mcp__playwright__", "")
                    .replace("mcp__gmail__", "gmail:")
                )
                desc = _tool_desc(name, block.get("input", {}))
                log_file.write(f"  >> {desc}\n")
                if on_action:
                    on_action(desc)
        return True

    if msg_type == "result":
        stats.update(
            {
                "input_tokens": msg.get("usage", {}).get("input_tokens", 0),
                "output_tokens": msg.get("usage", {}).get("output_tokens", 0),
                "cache_read": msg.get("usage", {}).get("cache_read_input_tokens", 0),
                "cache_create": msg.get("usage", {}).get("cache_creation_input_tokens", 0),
                "cost_usd": msg.get("total_cost_usd", 0),
                "turns": msg.get("num_turns", 0),
            }
        )
        result_text = msg.get("result", "")
        if result_text:
            text_parts.append(result_text)
            log_file.write(result_text + "\n")
        return True

    text = msg.get("text")
    if isinstance(text, str) and text:
        text_parts.append(text)
        log_file.write(text + "\n")
        return True

    return False


def _collect_process_output(
    proc: subprocess.Popen,
    worker_log: Path,
    on_action: ActionCallback | None,
) -> tuple[list[str], dict[str, Any]]:
    text_parts: list[str] = []
    stats: dict[str, Any] = {}

    if proc.stdout is None:
        return text_parts, stats

    with open(worker_log, "a", encoding="utf-8") as log_file:
        for raw_line in proc.stdout:
            line = raw_line.strip()
            if not line:
                continue
            if _handle_structured_line(line, text_parts, log_file, on_action, stats):
                continue
            text_parts.append(line)
            log_file.write(line + "\n")

    return text_parts, stats


class ClaudeCodeAgent(ApplyAgent):
    """Claude Code based apply agent."""

    backend = "claude"

    def __init__(self, timeout: int = _DEFAULT_TIMEOUT):
        self.timeout = timeout

    def run(
        self,
        *,
        prompt: str,
        model: str,
        worker_id: int,
        port: int,
        worker_dir: Path,
        mcp_config_path: Path,
        worker_log: Path,
        on_action: ActionCallback | None = None,
    ) -> AgentRunResult:
        cmd = [
            "claude",
            "--model",
            model,
            "-p",
            "--mcp-config",
            str(mcp_config_path),
            "--permission-mode",
            "bypassPermissions",
            "--no-session-persistence",
            "--disallowedTools",
            (
                "mcp__gmail__draft_email,mcp__gmail__modify_email,"
                "mcp__gmail__delete_email,mcp__gmail__download_attachment,"
                "mcp__gmail__batch_modify_emails,mcp__gmail__batch_delete_emails,"
                "mcp__gmail__create_label,mcp__gmail__update_label,"
                "mcp__gmail__delete_label,mcp__gmail__get_or_create_label,"
                "mcp__gmail__list_email_labels,mcp__gmail__create_filter,"
                "mcp__gmail__list_filters,mcp__gmail__get_filter,"
                "mcp__gmail__delete_filter"
            ),
            "--output-format",
            "stream-json",
            "--verbose",
            "-",
        ]

        env = os.environ.copy()
        env.pop("CLAUDECODE", None)
        env.pop("CLAUDE_CODE_ENTRYPOINT", None)

        start = time.time()
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(worker_dir),
        )
        with _agent_lock:
            _agent_procs[worker_id] = proc

        try:
            if proc.stdin is None:
                raise RuntimeError("Claude agent process missing stdin pipe")
            proc.stdin.write(prompt)
            proc.stdin.close()

            text_parts, stats = _collect_process_output(proc, worker_log, on_action)
            proc.wait(timeout=self.timeout)
            return AgentRunResult(
                output="\n".join(text_parts),
                duration_ms=int((time.time() - start) * 1000),
                stats=stats,
                skipped=bool(proc.returncode and proc.returncode < 0),
            )
        finally:
            with _agent_lock:
                _agent_procs.pop(worker_id, None)
            if proc.poll() is None:
                _kill_process_tree(proc.pid)


class CommandApplyAgent(ApplyAgent):
    """Generic command-backed apply agent for local or third-party runners."""

    backend = "command"

    def __init__(self, command: str, timeout: int = _DEFAULT_TIMEOUT):
        self.command = command
        self.timeout = timeout

    def _build_command(
        self,
        *,
        model: str,
        mcp_config_path: Path,
        worker_dir: Path,
        port: int,
        worker_id: int,
    ) -> list[str]:
        rendered = render_command_agent_command(
            self.command,
            model=model,
            mcp_config_path=mcp_config_path,
            worker_dir=worker_dir,
            port=port,
            worker_id=worker_id,
        )
        return shlex.split(rendered)

    def run(
        self,
        *,
        prompt: str,
        model: str,
        worker_id: int,
        port: int,
        worker_dir: Path,
        mcp_config_path: Path,
        worker_log: Path,
        on_action: ActionCallback | None = None,
    ) -> AgentRunResult:
        cmd = self._build_command(
            model=model,
            mcp_config_path=mcp_config_path,
            worker_dir=worker_dir,
            port=port,
            worker_id=worker_id,
        )
        if not cmd:
            raise ValueError("Apply agent command resolved to an empty command.")

        env = os.environ.copy()
        env["APPLYPILOT_MCP_CONFIG"] = str(mcp_config_path)
        env["APPLYPILOT_CDP_PORT"] = str(port)
        env["APPLYPILOT_MODEL"] = model
        env["APPLYPILOT_WORKER_DIR"] = str(worker_dir)
        env["APPLYPILOT_WORKER_ID"] = str(worker_id)

        start = time.time()
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(worker_dir),
        )
        with _agent_lock:
            _agent_procs[worker_id] = proc

        try:
            if proc.stdin is None:
                raise RuntimeError("Command agent process missing stdin pipe")
            proc.stdin.write(prompt)
            proc.stdin.close()

            text_parts, stats = _collect_process_output(proc, worker_log, on_action)
            proc.wait(timeout=self.timeout)
            return AgentRunResult(
                output="\n".join(text_parts),
                duration_ms=int((time.time() - start) * 1000),
                stats=stats,
                skipped=bool(proc.returncode and proc.returncode < 0),
            )
        finally:
            with _agent_lock:
                _agent_procs.pop(worker_id, None)
            if proc.poll() is None:
                _kill_process_tree(proc.pid)
