"""Backend execution helpers for individual agent runs."""

from __future__ import annotations

import json
import os
import queue
import shlex
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

_DEBUG_JSONL: bool = os.environ.get("AUTOMATOR_DEBUG_JSON") == "1"

from engine.work.progress import (
    capability_message,
    heartbeat_message,
    should_emit_heartbeat,
    stage_start_message,
)


def build_agent_command(
    agent_bin: str,
    prompt: str,
    *,
    session: Any = None,
) -> tuple[list[str], str | None]:
    """Build the CLI command for the agent.

    Returns (cmd, stdin_input). The prompt is piped via stdin to avoid
    OS argument-length limits (ARG_MAX). A short positional argument
    tells the CLI to read its full instructions from stdin.
    """
    parts = shlex.split(agent_bin)
    if not parts:
        raise ValueError(f"Invalid agent_bin: {agent_bin!r}")
    binary_name = parts[0].lower()
    cmd = [*parts]
    stdin_input: str | None = None

    if "gemini" in binary_name:
        if session and session.mode == "gemini_resume" and session.conversation_id:
            cmd.extend(["--resume", session.conversation_id])
        cmd.extend(["--prompt", "Follow the instructions provided via stdin exactly."])
        cmd.extend(["--output-format", "json"])
        stdin_input = prompt
    elif "claude" in binary_name:
        cmd.extend([
            "-p", "--dangerously-skip-permissions", "--output-format", "stream-json", "--verbose",
        ])
        if session and session.mode == "claude_session_id" and session.conversation_id:
            cmd.extend(["--session-id", session.conversation_id])
        cmd.append("Follow the instructions provided via stdin exactly.")
        stdin_input = prompt
    elif "codex" in binary_name:
        cmd.extend([
            "exec",
            "--skip-git-repo-check",
            "--sandbox",
            "workspace-write",
            "--json",
            "-",
        ])
        stdin_input = prompt
    else:
        cmd.extend(["exec", "--json", "-"])
        stdin_input = prompt

    return cmd, stdin_input


def runtime_check(
    agent_bin: str,
    *,
    runtime_check_prompt: str,
    build_agent_command: Callable[..., tuple[list[str], str | None]],
    extract_json_payload: Callable[[str], dict[str, Any]],
    runtime_check_output_has_success: Callable[[dict[str, Any], str], bool],
    timeout_seconds: int = 25,
    resolve_backend: Callable[..., Any] | None = None,
    runtime_check_api: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run a lightweight backend reachability probe using the engine adapter.

    If resolve_backend is provided and the backend is in API mode,
    delegates to runtime_check_api instead of running a CLI subprocess.
    """
    # --- API dispatch ---
    if resolve_backend is not None and runtime_check_api is not None:
        resolution = resolve_backend(agent_bin, "worker")
        if resolution.mode == "api":
            return runtime_check_api(
                resolution.backend_name,
                resolution.api_key,
                resolution.model,
                resolution.base_url,
            )

    # --- CLI path (unchanged) ---
    cmd, stdin_input = build_agent_command(agent_bin, runtime_check_prompt)
    try:
        proc = subprocess.run(
            cmd,
            input=stdin_input,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        def _decode(b: "bytes | str | None") -> str:
            if isinstance(b, bytes):
                return b.decode("utf-8", errors="replace")
            return b or ""
        output = (_decode(exc.stdout) + "\n" + _decode(exc.stderr)).strip()
        return {
            "backend": agent_bin,
            "ok": False,
            "reason": "timeout",
            "details": output[:500],
        }
    except OSError as exc:
        return {
            "backend": agent_bin,
            "ok": False,
            "reason": "launch_error",
            "details": str(exc),
        }

    full_text = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    payload = extract_json_payload(full_text)
    success = proc.returncode == 0 and runtime_check_output_has_success(payload, full_text)
    if success:
        return {
            "backend": agent_bin,
            "ok": True,
            "reason": "ok",
            "details": "",
        }
    return {
        "backend": agent_bin,
        "ok": False,
        "reason": f"exit_{proc.returncode}",
        "details": full_text[:500],
    }


def runtime_checks(
    backends: list[str],
    *,
    run_runtime_check: Callable[[str], dict[str, Any]],
) -> list[dict[str, Any]]:
    return [run_runtime_check(backend) for backend in backends]


def persist_result(
    project: dict[str, Any],
    role: str,
    result: dict[str, Any],
    *,
    write_json: Callable[[Path, Any], None],
) -> str:
    runtime_dir = Path(project["runtime_dir"])
    artifacts_dir = runtime_dir / "artifacts"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    artifact_name = f"{role}_result_{timestamp}.json"
    latest_name = f"latest_{role}.json"

    write_json(artifacts_dir / artifact_name, result)
    write_json(artifacts_dir / latest_name, result)
    return str(artifacts_dir / artifact_name)


def _emit_tool_progress(role: str, tool_name: str, tool_input: dict, emit_progress: Callable) -> None:
    """Emit a human-readable progress line for a single tool call."""
    name_lower = tool_name.lower().replace("_", "").replace("-", "")
    # ToolSearch is Claude's deferred-tool loader, not a user-visible search.
    # Label it as a tool-loading event so operators don't see confusing
    # "searching — select:Foo" lines leak through.
    if name_lower == "toolsearch":
        query = str(tool_input.get("query") or "")
        if query.startswith("select:"):
            targets = query[len("select:"):].strip() or "tool schemas"
            emit_progress(f"{role}: loading tool schemas ({targets})")
        else:
            emit_progress(f"{role}: loading tool schemas")
        return
    if any(k in name_lower for k in ("read", "view", "cat", "open")):
        path = tool_input.get("file_path") or tool_input.get("path") or tool_input.get("filename", "")
        emit_progress(f"{role}: reading {Path(path).name if path else 'file'}")
    elif any(k in name_lower for k in ("write", "edit", "create", "patch")):
        path = tool_input.get("file_path") or tool_input.get("path") or tool_input.get("filename", "")
        emit_progress(f"{role}: writing {Path(path).name if path else 'file'}")
    elif any(k in name_lower for k in ("search", "web", "google", "fetch", "browse")):
        query = tool_input.get("query") or tool_input.get("q", "")
        short = (query[:60] + "...") if len(query) > 60 else query
        emit_progress(f"{role}: searching — {short}" if short else f"{role}: web search")
    elif any(k in name_lower for k in ("bash", "shell", "command", "run", "exec")):
        cmd = str(tool_input.get("command") or tool_input.get("cmd", ""))
        short = (cmd[:60] + "...") if len(cmd) > 60 else cmd
        emit_progress(f"{role}: running {short}" if short else f"{role}: running command")
    elif any(k in name_lower for k in ("glob", "list", "ls", "find", "grep")):
        emit_progress(f"{role}: searching files")
    else:
        emit_progress(f"{role}: {tool_name}")


def _parse_event_progress(role: str, line: str, emit_progress: Callable) -> None:
    """Parse a single JSONL line and emit progress if it represents a tool action."""
    if not line.startswith("{"):
        return
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return
    etype = event.get("type", "")
    # Claude CLI stream-json: assistant message with tool_use content blocks
    if etype == "assistant":
        msg = event.get("message") or {}
        for block in msg.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                _emit_tool_progress(role, block.get("name", "tool"), block.get("input") or {}, emit_progress)
        return
    # Claude SDK streaming: content_block_start with tool_use
    if etype == "content_block_start":
        cb = event.get("content_block", {})
        if cb.get("type") == "tool_use":
            emit_progress(f"{role}: {cb.get('name', 'tool')}")
        return
    # Claude streaming: standalone tool_use block
    if etype == "tool_use":
        _emit_tool_progress(role, event.get("name", "tool"), event.get("input", {}), emit_progress)
        return
    # Gemini / generic: functionCall
    if etype in ("functionCall", "function_call", "tool_call"):
        name = event.get("name") or (event.get("functionCall") or {}).get("name", "tool")
        inp = event.get("args") or event.get("input") or (event.get("functionCall") or {}).get("args", {}) or {}
        _emit_tool_progress(role, name, inp, emit_progress)
        return
    # Gemini streaming: candidates with functionCall parts
    for cand in event.get("candidates", []):
        for part in (cand.get("content") or {}).get("parts", []):
            fc = part.get("functionCall")
            if fc:
                _emit_tool_progress(role, fc.get("name", "tool"), fc.get("args", {}), emit_progress)
                return
    # Codex JSONL: item.started/item.completed for command_execution and similar tool-like items
    if etype in ("item.started", "item.completed"):
        item = event.get("item") or {}
        item_type = item.get("type", "")
        if item_type == "agent_message":
            return
        tool_input: dict[str, Any] = {}
        if isinstance(item.get("command"), str):
            tool_input["command"] = item.get("command")
        if isinstance(item.get("path"), str):
            tool_input["path"] = item.get("path")
        if isinstance(item.get("args"), dict):
            tool_input.update(item.get("args") or {})
        if isinstance(item.get("input"), dict):
            tool_input.update(item.get("input") or {})
        _emit_tool_progress(role, item_type or "tool", tool_input, emit_progress)
        return


def _count_native_tool_uses(raw_output: str) -> int:
    """Count real tool invocations in a backend's streaming output.

    Native tool_use events come from the provider SDK (Claude stream-json,
    Codex item.completed, Gemini functionCall) — they represent real work
    the agent performed. The review auto-demote uses this to distinguish
    "pass without running any check" from "pass after using native Bash."
    """
    count = 0
    for line in raw_output.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = event.get("type", "")
        if etype == "assistant":
            msg = event.get("message") or {}
            for block in msg.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    count += 1
            continue
        if etype == "tool_use":
            count += 1
            continue
        if etype == "content_block_start":
            cb = event.get("content_block", {}) or {}
            if cb.get("type") == "tool_use":
                count += 1
            continue
        if etype in ("functionCall", "function_call", "tool_call"):
            count += 1
            continue
        if etype == "item.completed":
            item = event.get("item") or {}
            item_type = item.get("type", "")
            if item_type and item_type != "agent_message":
                # command_execution, file_read, etc. — real work
                count += 1
            continue
    return count


def _stream_process(
    proc: subprocess.Popen,
    stdin_input: str | None,
    role: str,
    start_time: datetime,
    spawn_timeout_seconds: int,
    emit_progress: Callable,
) -> tuple[str, str, bool]:
    """Read process stdout line-by-line, emitting progress for tool events.

    Returns (stdout_text, stderr_text, timed_out).
    The heartbeat fires only when no new output has arrived for 30 seconds,
    so active tool-use replaces the heartbeat naturally.
    """
    line_q: queue.Queue[tuple[str, str | None]] = queue.Queue()

    def _reader() -> None:
        try:
            if proc.stdout is None:
                return
            for raw in proc.stdout:
                line_q.put(("line", raw.rstrip("\n")))
        finally:
            line_q.put(("done", None))

    threading.Thread(target=_reader, daemon=True).start()

    if stdin_input is not None and proc.stdin is not None:
        try:
            proc.stdin.write(stdin_input)
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass  # stdin was closed by the subprocess before we finished writing; non-fatal

    collected: list[str] = []
    last_heartbeat_at: datetime | None = None
    timed_out = False

    while True:
        try:
            kind, data = line_q.get(timeout=1)
        except queue.Empty:
            now = datetime.now(timezone.utc)
            if should_emit_heartbeat(start_time, last_heartbeat_at, now=now):
                emit_progress(heartbeat_message(role, start_time, now=now))
                last_heartbeat_at = now
            if (now - start_time).total_seconds() >= spawn_timeout_seconds:
                proc.kill()
                timed_out = True
                break
            continue

        # Check timeout even when output is flowing — a chatty process must
        # not bypass the timeout simply by producing continuous output.
        if (datetime.now(timezone.utc) - start_time).total_seconds() >= spawn_timeout_seconds:
            proc.kill()
            timed_out = True
            break

        if kind == "done":
            break

        if data is None:
            continue
        collected.append(data)
        # Reset heartbeat timer — visible activity replaces the fallback message
        last_heartbeat_at = datetime.now(timezone.utc)
        _parse_event_progress(role, data, emit_progress)

    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        timed_out = True
    stderr = ""
    if proc.stderr:
        # Use a thread to avoid hanging if child processes inherited stderr fd.
        stderr_q: queue.Queue[str] = queue.Queue()
        def _read_stderr() -> None:
            try:
                stderr_q.put(proc.stderr.read() if proc.stderr else "")
            except Exception:
                stderr_q.put("")
        t = threading.Thread(target=_read_stderr, daemon=True)
        t.start()
        t.join(timeout=5)
        if not stderr_q.empty():
            stderr = stderr_q.get_nowait()
    return "\n".join(collected), stderr, timed_out


def run_agent(
    role: str,
    task: str,
    reason: str,
    assignment_mode: str | None,
    inputs: list[str],
    project: dict[str, Any] | None,
    agent_bin: str,
    *,
    delivery_mode: str | None = None,
    force_full_artifacts: list[str] | None,
    expected_result_shape: dict[str, Any] | None,
    session: Any,
    build_prompt: Callable[..., str],
    estimate_tokens: Callable[[str], int],
    build_agent_command: Callable[..., tuple[list[str], str | None]],
    is_toon_available: Callable[[], bool],
    emit_progress: Callable[[str], None],
    repo_root: Any,
    spawn_timeout_seconds: int,
    classify_error: Callable[[str], str],
    extract_session_id_from_text: Callable[[str], str | None],
    extract_json_payload: Callable[[str], dict[str, Any]],
    resolve_backend: Callable[..., Any] | None = None,
    run_agent_api: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    # --- API dispatch ---
    if resolve_backend is not None and run_agent_api is not None:
        resolution = resolve_backend(agent_bin, role)
        if resolution.mode == "api":
            return run_agent_api(
                role,
                task,
                reason,
                assignment_mode,
                inputs,
                project,
                delivery_mode=delivery_mode,
                backend_name=resolution.backend_name,
                model=resolution.model,
                api_key=resolution.api_key,
                base_url=resolution.base_url,
                timeout_seconds=resolution.timeout_seconds or spawn_timeout_seconds,
                session=session,
                force_full_artifacts=force_full_artifacts,
                expected_result_shape=expected_result_shape,
                build_prompt=build_prompt,
                estimate_tokens=estimate_tokens,
                is_toon_available=is_toon_available,
                emit_progress=emit_progress,
                extract_json_payload=extract_json_payload,
                classify_error=classify_error,
            )

    # --- CLI path (unchanged) ---
    prompt = build_prompt(
        role,
        task,
        reason,
        inputs,
        project,
        assignment_mode,
        delivery_mode,
        force_full_artifacts,
        expected_result_shape,
        session=session,
    )
    prompt_tokens = estimate_tokens(prompt)
    cmd, stdin_input = build_agent_command(agent_bin, prompt, session=session)
    emit_progress(stage_start_message(role, task, prompt_tokens=prompt_tokens))

    cwd = project["project_root"] if project else repo_root
    start_time = datetime.now(timezone.utc)
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdin=subprocess.PIPE if stdin_input is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout, stderr, timed_out = _stream_process(
            proc, stdin_input, role, start_time, spawn_timeout_seconds, emit_progress
        )
        if timed_out:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=spawn_timeout_seconds)

        duration = (datetime.now(timezone.utc) - start_time).total_seconds()

        if proc.returncode != 0:
            parts = []
            if stderr and stderr.strip():
                parts.append(f"stderr: {stderr.strip()}")
            if stdout and stdout.strip():
                parts.append(f"stdout: {stdout.strip()[:2000]}")
            error_msg = "; ".join(parts) if parts else f"Exit code {proc.returncode}"
            return {"status": "failed", "error": error_msg, "error_category": classify_error(error_msg)}

        raw_output = stdout.strip()
        lines = raw_output.splitlines()
        is_jsonl = any(line.strip().startswith('{"') for line in lines[:5])

        full_text = ""
        if is_jsonl:
            message_fragments = []
            for line in lines:
                try:
                    event = json.loads(line)
                    if event.get("type") == "result" and "result" in event:
                        if event.get("is_error"):
                            raw_result = event.get("result", "Agent returned an error")
                            error_msg = str(raw_result) if raw_result else "Agent returned an error"
                            return {"status": "failed", "error": error_msg, "error_category": classify_error(error_msg)}
                        full_text = event.get("result", "")
                        break
                    if event.get("type") == "item.completed":
                        item = event.get("item", {})
                        if item.get("type") == "agent_message":
                            text = item.get("text", "")
                            if text:
                                message_fragments.append(text)
                except json.JSONDecodeError:
                    if _DEBUG_JSONL:
                        print(f"[execution] skipping non-JSON JSONL line: {line[:120]!r}", file=sys.stderr)
                    pass  # skip non-JSON lines in JSONL stream
            if not full_text:
                full_text = "".join(message_fragments)
        else:
            full_text = "\n".join(lines)

        if not full_text:
            return {
                "status": "failed",
                "error": "No output text found in agent response",
                "error_category": "invalid_output",
            }

        # Session IDs for JSONL-speaking CLIs like Codex are often emitted in
        # early lifecycle events (for example thread.started) rather than in the
        # final agent message. Preserve the full raw stream for extraction.
        session_id = extract_session_id_from_text(raw_output if is_jsonl else full_text)
        if session is not None and session.persistent:
            if session_id and not session.conversation_id and session.mode != "codex_portable":
                session.conversation_id = session_id

        payload = extract_json_payload(full_text)
        if not payload:
            error_msg = f"Invalid JSON in final message: {full_text[:200]}..."
            return {"status": "failed", "error": error_msg, "error_category": "invalid_output"}

        native_tool_uses = _count_native_tool_uses(raw_output) if is_jsonl else 0
        capability_requests = payload.get("capability_requests", [])
        if capability_requests:
            return {
                "status": "capability_requested",
                "output": payload,
                "capability_requests": capability_requests,
                "duration": duration,
                "native_tool_uses": native_tool_uses,
            }

        return {
            "status": "success",
            "output": payload,
            "duration": duration,
            "native_tool_uses": native_tool_uses,
        }
    except OSError as exc:
        error_msg = str(exc)
        return {"status": "failed", "error": error_msg, "error_category": classify_error(error_msg)}
    except subprocess.TimeoutExpired:
        return {
            "status": "failed",
            "error": f"Timed out after {spawn_timeout_seconds}s",
            "error_category": "timeout",
        }


def run_agent_with_capabilities(
    role: str,
    task: str,
    reason: str,
    assignment_mode: str | None,
    inputs: list[str],
    project: dict[str, Any] | None,
    agent_bin: str,
    *,
    delivery_mode: str | None = None,
    force_full_artifacts: list[str] | None,
    expected_result_shape: dict[str, Any] | None,
    session: Any,
    run_agent: Callable[..., dict[str, Any]],
    max_capability_rounds: int,
    validate_capability_request: Callable[[dict[str, Any]], list[str]],
    emit_progress: Callable[[str], None],
    execute_capability: Callable[[dict[str, Any]], dict[str, Any]],
    serialize_for_prompt: Callable[[Any], str],
) -> dict[str, Any]:
    res = run_agent(
        role,
        task,
        reason,
        assignment_mode,
        inputs,
        project,
        agent_bin,
        delivery_mode=delivery_mode,
        force_full_artifacts=force_full_artifacts,
        expected_result_shape=expected_result_shape,
        session=session,
    )
    capability_round = 0
    native_tool_uses_total = int(res.get("native_tool_uses", 0) or 0)
    # Each entry: (round_number, full_text, compact_summary)
    all_rounds: list[tuple[int, str, str]] = []
    # Number of recent rounds to keep in full detail; older rounds get compacted.
    _FULL_DETAIL_ROUNDS = 2
    _MAX_CUMULATIVE_CHARS = 200_000
    while res["status"] == "capability_requested" and capability_round < max_capability_rounds:
        cap_requests = res.get("capability_requests", [])
        if not cap_requests:
            # Agent returned capability_requested with no actual requests — treat as success
            res["status"] = "success"
            break
        capability_round += 1
        cap_results = []
        for cap_req in cap_requests:
            req_warnings = validate_capability_request(cap_req)
            for warning in req_warnings:
                emit_progress(f"[engine] Capability request warning: {warning}")
            if "capability" not in cap_req:
                cap_results.append({
                    "capability": "unknown",
                    "status": "failed",
                    "result": None,
                    "issues": ["Malformed capability request: missing 'capability' field"],
                })
                continue
            emit_progress(capability_message(role, str(cap_req.get("capability", "?"))))
            cap_result = execute_capability(cap_req)
            cap_results.append(cap_result)
        cap_results_str = serialize_for_prompt(cap_results)
        full_text = f"Round {capability_round} results:\n{cap_results_str}"
        # Build a compact one-line summary per capability in this round.
        compact_lines = []
        for cr in cap_results:
            cap_name = cr.get("capability", "?")
            cap_status = cr.get("status", "?")
            issues = cr.get("issues", [])
            issue_hint = f" — {issues[0][:80]}" if issues else ""
            compact_lines.append(f"  {cap_name}: {cap_status}{issue_hint}")
        compact_summary = f"Round {capability_round} (compact): " + "; ".join(
            f"{cr.get('capability', '?')}={cr.get('status', '?')}" for cr in cap_results
        )
        all_rounds.append((capability_round, full_text, compact_summary))

        # Build cumulative context: compact older rounds, full detail for recent ones.
        parts: list[str] = []
        cutoff = len(all_rounds) - _FULL_DETAIL_ROUNDS
        if cutoff > 0:
            parts.append("Prior rounds (compacted):")
            for rnd_num, _full, summary in all_rounds[:cutoff]:
                parts.append(f"  {summary}")
            parts.append("")  # blank line separator
        for _rnd_num, full, _summary in all_rounds[max(0, cutoff):]:
            parts.append(full)
        cumulative_results = "\n".join(parts)

        # Hard ceiling: if still over budget, drop oldest compacted entries.
        if len(cumulative_results) > _MAX_CUMULATIVE_CHARS:
            kept_full = [full for _, full, _ in all_rounds[-_FULL_DETAIL_ROUNDS:]]
            cumulative_results = (
                f"[engine] Note: earlier rounds dropped to fit context budget.\n\n"
                + "\n\n".join(kept_full)
            )
        augmented_task = (
            f"{task}\n\n"
            f"Runtime Capability Results ({capability_round} round(s) so far):\n{cumulative_results}\n\n"
            f"Use these results to complete your task. Do NOT re-request capabilities whose results you already have."
        )
        res = run_agent(
            role,
            augmented_task,
            reason,
            assignment_mode,
            inputs,
            project,
            agent_bin,
            delivery_mode=delivery_mode,
            force_full_artifacts=force_full_artifacts,
            expected_result_shape=expected_result_shape,
            session=session,
        )
        native_tool_uses_total += int(res.get("native_tool_uses", 0) or 0)
    if res["status"] == "capability_requested":
        emit_progress(
            f"[engine] Capability re-invocation limit reached ({max_capability_rounds} rounds) for {role}. Treating as failure."
        )
        res = {
            "status": "failed",
            "error": f"Specialist {role} kept requesting capabilities after {max_capability_rounds} rounds",
            "error_category": "capability_loop",
        }
    # Expose the actual number of capability rounds executed so callers can
    # distinguish "the agent ran real checks" from "the agent self-reported
    # without running any capability" — used by the review auto-demote.
    # native_tool_uses covers the path where the agent uses Claude's built-in
    # Bash/Read tools instead of routing through the engine capability envelope.
    res["capability_rounds_used"] = capability_round
    res["native_tool_uses"] = native_tool_uses_total
    return res
