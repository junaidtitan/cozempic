"""Agent team state extraction, checkpointing, and recovery injection.

Scans JSONL session files for agent team coordination patterns:
- Task tool calls (subagent spawns with subagent_type, prompt, description)
- task-notification messages (actual agent results, status, summaries)
- TaskCreate/TaskUpdate/TaskList/TaskGet (shared todo list)
- TaskOutput (background agent results)
- TeamCreate/SendMessage (explicit team coordination)

Injects team state back into a pruned session so that Claude resumes
with full team awareness.
"""

from __future__ import annotations

import json
import re
import uuid as uuid_mod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .types import Message


@dataclass
class SubagentInfo:
    """Information about a spawned subagent (Task tool call)."""

    agent_id: str
    description: str = ""
    subagent_type: str = ""
    status: str = "running"  # running, completed, failed
    result_summary: str = ""


@dataclass
class TeammateInfo:
    """Information about a named teammate (explicit team)."""

    agent_id: str
    name: str
    role: str = ""
    status: str = "unknown"  # running, done, idle


@dataclass
class TaskInfo:
    """Information about a task in the shared task list."""

    task_id: str
    subject: str
    status: str = "pending"
    owner: str = ""
    description: str = ""


@dataclass
class TeamState:
    """Extracted state of an agent team from conversation history."""

    team_name: str = ""
    teammates: list[TeammateInfo] = field(default_factory=list)
    subagents: list[SubagentInfo] = field(default_factory=list)
    tasks: list[TaskInfo] = field(default_factory=list)
    lead_summary: str = ""
    message_count: int = 0
    last_coordination_index: int = -1

    def is_empty(self) -> bool:
        return (
            not self.team_name
            and not self.teammates
            and not self.subagents
            and not self.tasks
        )

    def to_markdown(self) -> str:
        """Render team state as markdown for checkpoint file."""
        lines = []
        lines.append(f"# Agent Team Checkpoint: {self.team_name or 'unnamed'}")
        lines.append(f"_Generated: {datetime.now().isoformat()}_")
        lines.append("")

        if self.teammates:
            lines.append("## Teammates")
            for t in self.teammates:
                status = f" ({t.status})" if t.status != "unknown" else ""
                role = f" — {t.role}" if t.role else ""
                lines.append(f"- **{t.name}** (`{t.agent_id}`){role}{status}")
            lines.append("")

        if self.subagents:
            lines.append("## Subagents")
            for s in self.subagents:
                agent_type = f" [{s.subagent_type}]" if s.subagent_type else ""
                desc = f" — {s.description}" if s.description else ""
                lines.append(f"- `{s.agent_id}`{agent_type}{desc} ({s.status})")
                if s.result_summary:
                    lines.append(f"  Result: {s.result_summary[:200]}")
            lines.append("")

        if self.tasks:
            lines.append("## Task List")
            status_icons = {"completed": "x", "in_progress": "/", "pending": " "}
            for t in self.tasks:
                icon = status_icons.get(t.status, " ")
                owner = f" @{t.owner}" if t.owner else ""
                lines.append(f"- [{icon}] {t.subject}{owner}")
                if t.description:
                    lines.append(f"  {t.description[:200]}")
            lines.append("")

        if self.lead_summary:
            lines.append("## Lead Context")
            lines.append(self.lead_summary)
            lines.append("")

        total = self.message_count
        lines.append(f"_Extracted from {total} team-related messages_")
        return "\n".join(lines)

    def to_recovery_text(self) -> str:
        """Render team state as text for injection into conversation."""
        parts = []
        parts.append(f"Active agent team: {self.team_name or 'unnamed'}")

        if self.teammates:
            parts.append("\nTeammates:")
            for t in self.teammates:
                role = f" — {t.role}" if t.role else ""
                parts.append(f"  - {t.name} (agent_id: {t.agent_id}){role} [{t.status}]")

        if self.subagents:
            parts.append(f"\nSubagents ({len(self.subagents)}):")
            for s in self.subagents:
                agent_type = f" [{s.subagent_type}]" if s.subagent_type else ""
                desc = f" — {s.description}" if s.description else ""
                parts.append(f"  - {s.agent_id}{agent_type}{desc} [{s.status}]")
                if s.result_summary:
                    parts.append(f"    Result: {s.result_summary[:150]}")

        if self.tasks:
            parts.append("\nShared task list:")
            for t in self.tasks:
                owner = f" (owner: {t.owner})" if t.owner else ""
                parts.append(f"  - [{t.status.upper()}] {t.subject}{owner}")

        if self.lead_summary:
            parts.append(f"\nCoordination context: {self.lead_summary}")

        return "\n".join(parts)


# ─── Patterns for team message detection ─────────────────────────────────────

# Tool names that indicate team/agent coordination
TEAM_TOOL_NAMES = {
    # Explicit team coordination
    "TeamCreate", "TeamDelete", "TeamMessage", "SendMessage",
    "SpawnTeammate", "TeamStatus",
    # Shared task list (todo tracking)
    "TaskCreate", "TaskUpdate", "TaskList", "TaskGet",
    # Subagent spawning and results (Claude Code's Task tool)
    "Task", "TaskOutput", "TaskStop",
}

# Keyword patterns for detecting team-related content in text/results
TEAM_KEYWORDS = re.compile(
    r"team.?name|agent.?id|teammate|team.?lead|"
    r"SendMessage|TeamCreate|TaskCreate|TaskUpdate|"
    r"agent.?team|spawn.+teammate|team.+config|"
    r"subagent_type|run_in_background|resume.*agent",
    re.IGNORECASE,
)

# Patterns for parsing task-notification XML in user messages
_TASK_NOTIFICATION_RE = re.compile(
    r"<task-notification>\s*"
    r"<task-id>([^<]+)</task-id>\s*"
    r"<status>([^<]+)</status>\s*"
    r"<summary>([^<]*)</summary>\s*"
    r"<result>(.*?)</result>",
    re.DOTALL,
)

# Pattern for agent progress notifications in system-reminder tags
_AGENT_PROGRESS_RE = re.compile(
    r"Agent\s+([a-f0-9]+)\s+progress:.*?(\d+)\s+new\s+tool",
    re.IGNORECASE,
)


def _is_team_message(msg_dict: dict) -> bool:
    """Check if a message is related to agent team coordination.

    Detects:
    - Task tool calls (subagent spawns)
    - task-notification messages (agent completion results)
    - TaskCreate/Update/List/Get (todo list)
    - TaskOutput/TaskStop (background agent management)
    - TeamCreate/SendMessage (explicit teams)
    - Tool results from any of the above
    - Text mentioning team coordination keywords
    """
    inner = msg_dict.get("message", {})
    content = inner.get("content", [])

    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue

            block_type = block.get("type", "")

            # Tool use with team-related name
            if block_type == "tool_use" and block.get("name") in TEAM_TOOL_NAMES:
                return True

            # Tool result — check both name reference and content
            if block_type == "tool_result":
                result_content = block.get("content", "")
                if isinstance(result_content, str) and TEAM_KEYWORDS.search(result_content):
                    return True
                if isinstance(result_content, list):
                    for sub in result_content:
                        if isinstance(sub, dict):
                            text = sub.get("text", "")
                            if isinstance(text, str) and TEAM_KEYWORDS.search(text):
                                return True

            # Text mentioning team coordination
            if block_type == "text":
                text = block.get("text", "")
                if isinstance(text, str) and TEAM_KEYWORDS.search(text):
                    return True

    elif isinstance(content, str):
        # task-notification XML in user messages (agent results)
        if "<task-notification>" in content:
            return True
        if TEAM_KEYWORDS.search(content):
            return True

    return False


def _is_task_tool_result(msg_dict: dict, pending_task_ids: set[str]) -> bool:
    """Check if a message contains a tool_result for a Task tool call.

    Task tool results carry the agent's output — these are critical to preserve.
    """
    inner = msg_dict.get("message", {})
    content = inner.get("content", [])

    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                tool_use_id = block.get("tool_use_id", "")
                if tool_use_id in pending_task_ids:
                    return True

    return False


def extract_team_state(messages: list[Message]) -> TeamState:
    """Scan messages for team coordination patterns and extract state.

    Looks for:
    - Task tool calls (subagent spawns with subagent_type, prompt, description)
    - TaskOutput calls (checking on background agents)
    - TeamCreate tool calls (team name, teammate configs)
    - SendMessage / TeamMessage tool calls
    - TaskCreate / TaskUpdate tool calls (shared todo list)
    - Teammate spawn details (agent IDs, roles)
    """
    state = TeamState()
    seen_teammates: dict[str, TeammateInfo] = {}
    seen_subagents: dict[str, SubagentInfo] = {}
    seen_tasks: dict[str, TaskInfo] = {}

    # Track tool_use_id -> tool_name for matching results to calls
    tool_use_id_to_name: dict[str, str] = {}
    # Track tool_use_id -> subagent key for Task tool results
    tool_use_id_to_subagent: dict[str, str] = {}

    for line_idx, msg, byte_size in messages:
        if not _is_team_message(msg):
            continue

        state.message_count += 1
        state.last_coordination_index = line_idx

        inner = msg.get("message", {})
        content = inner.get("content", [])
        if not isinstance(content, list):
            continue

        for block in content:
            if not isinstance(block, dict):
                continue

            block_type = block.get("type", "")

            # ── Tool use blocks ──────────────────────────────────────
            if block_type == "tool_use":
                name = block.get("name", "")
                inp = block.get("input", {})
                tool_use_id = block.get("id", "")

                if tool_use_id and name:
                    tool_use_id_to_name[tool_use_id] = name

                # Task tool = subagent spawn
                if name == "Task":
                    description = inp.get("description", "")
                    subagent_type = inp.get("subagent_type", "")
                    prompt = inp.get("prompt", "")[:200]
                    resume_id = inp.get("resume", "")
                    bg = inp.get("run_in_background", False)

                    # Use tool_use_id as temporary key until we get agent_id
                    key = resume_id or tool_use_id or f"task-{len(seen_subagents)}"
                    agent = SubagentInfo(
                        agent_id=key,
                        description=description or prompt[:80],
                        subagent_type=subagent_type,
                        status="running" if bg else "running",
                    )
                    seen_subagents[key] = agent
                    if tool_use_id:
                        tool_use_id_to_subagent[tool_use_id] = key

                    # Infer team name from subagent_type if not set
                    if not state.team_name and subagent_type:
                        state.team_name = f"agents"

                # TaskOutput = checking on background agent
                elif name == "TaskOutput":
                    task_id = inp.get("task_id", "")
                    if task_id and task_id in seen_subagents:
                        # Still running, waiting for result
                        pass

                # TaskStop = stopping a background agent
                elif name == "TaskStop":
                    task_id = inp.get("task_id", "")
                    if task_id and task_id in seen_subagents:
                        seen_subagents[task_id].status = "stopped"

                # TeamCreate (explicit team)
                elif name == "TeamCreate":
                    state.team_name = inp.get("name", state.team_name)
                    for tm in inp.get("teammates", []):
                        agent_id = tm.get("agentId", tm.get("agent_id", ""))
                        tm_name = tm.get("name", agent_id)
                        role = tm.get("role", tm.get("description", ""))
                        if agent_id:
                            seen_teammates[agent_id] = TeammateInfo(
                                agent_id=agent_id,
                                name=tm_name,
                                role=role,
                                status="running",
                            )

                # TaskCreate (shared todo list)
                elif name == "TaskCreate":
                    task_id = inp.get("taskId", inp.get("id", str(len(seen_tasks))))
                    subject = inp.get("subject", inp.get("title", ""))
                    seen_tasks[task_id] = TaskInfo(
                        task_id=task_id,
                        subject=subject,
                        status="pending",
                        owner=inp.get("owner", ""),
                        description=inp.get("description", ""),
                    )

                # TaskUpdate (shared todo list)
                elif name == "TaskUpdate":
                    task_id = inp.get("taskId", inp.get("id", ""))
                    if task_id in seen_tasks:
                        if inp.get("status"):
                            seen_tasks[task_id].status = inp["status"]
                        if inp.get("owner"):
                            seen_tasks[task_id].owner = inp["owner"]
                        if inp.get("subject"):
                            seen_tasks[task_id].subject = inp["subject"]
                    else:
                        seen_tasks[task_id] = TaskInfo(
                            task_id=task_id,
                            subject=inp.get("subject", ""),
                            status=inp.get("status", "unknown"),
                            owner=inp.get("owner", ""),
                        )

                elif name in ("SendMessage", "TeamMessage"):
                    target = inp.get("to", inp.get("agentId", ""))
                    if target and target in seen_teammates:
                        seen_teammates[target].status = "running"

            # ── Tool result blocks ───────────────────────────────────
            elif block_type == "tool_result":
                tool_use_id = block.get("tool_use_id", "")
                tool_name = tool_use_id_to_name.get(tool_use_id, "")

                # Task tool result = subagent finished, capture result
                if tool_name == "Task" or tool_use_id in tool_use_id_to_subagent:
                    subagent_key = tool_use_id_to_subagent.get(tool_use_id, "")
                    result_text = ""

                    result_content = block.get("content", "")
                    if isinstance(result_content, str):
                        result_text = result_content
                    elif isinstance(result_content, list):
                        for sub in result_content:
                            if isinstance(sub, dict) and sub.get("type") == "text":
                                result_text += sub.get("text", "")

                    if subagent_key and subagent_key in seen_subagents:
                        seen_subagents[subagent_key].status = "completed"
                        seen_subagents[subagent_key].result_summary = result_text[:300]

                    # Check if result contains an agent_id we should track
                    agent_id_match = re.search(r"agent[_-]?id[:\s]+([a-f0-9-]+)", result_text, re.I)
                    if agent_id_match and subagent_key and subagent_key in seen_subagents:
                        real_id = agent_id_match.group(1)
                        agent = seen_subagents.pop(subagent_key)
                        agent.agent_id = real_id
                        seen_subagents[real_id] = agent

    # ── Second pass: scan for task-notification messages ────────────
    # These are user messages containing XML with actual agent results,
    # delivered after background agents complete. They carry the real
    # result text (not just "Async agent launched").
    for line_idx, msg, byte_size in messages:
        inner = msg.get("message", {})
        content = inner.get("content", "")

        # task-notifications are string content in user messages
        if not isinstance(content, str) or "<task-notification>" not in content:
            continue

        for match in _TASK_NOTIFICATION_RE.finditer(content):
            task_id = match.group(1).strip()
            status = match.group(2).strip()
            summary = match.group(3).strip()
            result = match.group(4).strip()

            # Find the matching subagent by agent_id
            if task_id in seen_subagents:
                seen_subagents[task_id].status = status
                seen_subagents[task_id].result_summary = result[:300]
                if summary and not seen_subagents[task_id].description:
                    seen_subagents[task_id].description = summary
            else:
                # Agent was spawned but we only have the notification
                seen_subagents[task_id] = SubagentInfo(
                    agent_id=task_id,
                    description=summary,
                    status=status,
                    result_summary=result[:300],
                )

            state.message_count += 1

    state.teammates = list(seen_teammates.values())
    state.subagents = list(seen_subagents.values())
    state.tasks = list(seen_tasks.values())

    # Build lead summary from last few team-related assistant messages
    team_msgs: list[str] = []
    for line_idx, msg, byte_size in messages:
        if msg.get("type") == "assistant" and _is_team_message(msg):
            inner = msg.get("message", {})
            content = inner.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        team_msgs.append(block.get("text", "")[:300])

    if team_msgs:
        state.lead_summary = " [...] ".join(team_msgs[-3:])

    return state


def write_team_checkpoint(state: TeamState, project_dir: Path | None = None) -> Path:
    """Write team state checkpoint to disk.

    Writes to .claude/team-checkpoint.md in the project directory,
    or to ~/.claude/team-checkpoint.md as fallback.
    """
    if project_dir and project_dir.exists():
        path = project_dir / "team-checkpoint.md"
    else:
        path = Path.home() / ".claude" / "team-checkpoint.md"

    path.write_text(state.to_markdown())
    return path


def inject_team_recovery(messages: list[Message], state: TeamState) -> list[Message]:
    """Inject team state as a synthetic message pair at the end of the session.

    Appends:
    1. A 'user' message asking about team state
    2. An 'assistant' message confirming the full team state

    This ensures that when Claude resumes from the pruned JSONL,
    it 'remembers' the team — not as a suggestion but as actual
    conversation history.
    """
    if state.is_empty():
        return messages

    # Find the last message to chain UUIDs
    last_uuid = None
    last_session_id = None
    last_cwd = None
    last_git_branch = None

    for _, msg, _ in reversed(messages):
        if msg.get("uuid"):
            last_uuid = msg["uuid"]
            last_session_id = msg.get("sessionId")
            last_cwd = msg.get("cwd")
            last_git_branch = msg.get("gitBranch")
            break

    if not last_uuid:
        return messages  # Can't chain without a UUID

    now = datetime.now().isoformat()
    user_uuid = str(uuid_mod.uuid4())
    assistant_uuid = str(uuid_mod.uuid4())

    recovery_text = state.to_recovery_text()
    checkpoint_note = (
        "A team state checkpoint was also written to .claude/team-checkpoint.md."
    )

    # User message: trigger for team state recovery
    user_msg = {
        "type": "user",
        "uuid": user_uuid,
        "parentUuid": last_uuid,
        "sessionId": last_session_id,
        "timestamp": now,
        "cwd": last_cwd,
        "gitBranch": last_git_branch,
        "isSidechain": False,
        "userType": "external",
        "message": {
            "role": "user",
            "content": (
                "[Cozempic Guard: Context was pruned to prevent compaction. "
                "Confirm the current agent team state below.]\n\n"
                f"{recovery_text}"
            ),
        },
    }

    # Assistant message: confirms team state
    assistant_msg = {
        "type": "assistant",
        "uuid": assistant_uuid,
        "parentUuid": user_uuid,
        "sessionId": last_session_id,
        "timestamp": now,
        "cwd": last_cwd,
        "gitBranch": last_git_branch,
        "isSidechain": False,
        "userType": "external",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"Confirmed — I have an active agent team. {recovery_text}\n\n"
                        f"{checkpoint_note}\n\n"
                        "Continuing with team coordination."
                    ),
                }
            ],
        },
    }

    user_line = json.dumps(user_msg, separators=(",", ":"))
    assistant_line = json.dumps(assistant_msg, separators=(",", ":"))

    # Append as new messages at the end
    next_idx = max(idx for idx, _, _ in messages) + 1 if messages else 0
    messages = list(messages)  # copy
    messages.append((next_idx, user_msg, len(user_line.encode("utf-8"))))
    messages.append((next_idx + 1, assistant_msg, len(assistant_line.encode("utf-8"))))

    return messages
