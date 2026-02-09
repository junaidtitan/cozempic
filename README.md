# Cozempic

Context cleaning for [Claude Code](https://claude.ai/code) — **remove the bloat, keep everything that matters, protect Agent Teams from context loss**.

### What gets removed

Claude Code context fills up with dead weight that wastes your token budget: hundreds of progress tick messages, repeated thinking blocks and signatures, stale file reads that were superseded by edits, duplicate document injections, oversized tool outputs, and metadata bloat (token counts, stop reasons, cost fields). A typical session carries 8-46MB — most of it noise. Cozempic identifies and removes all of it using 13 composable strategies, while your actual conversation, decisions, tool results, and working context stay untouched.

### Agent Teams context loss protection

When context gets too large, Claude's auto-compaction summarizes away critical state. For **Agent Teams**, this is catastrophic: the lead agent's context is compacted, team coordination messages (TeamCreate, SendMessage, TaskCreate/Update) are discarded, the lead forgets its teammates exist, and subagents are orphaned with no recovery path. ([#23620](https://github.com/anthropics/claude-code/issues/23620), [#23821](https://github.com/anthropics/claude-code/issues/23821), [#24052](https://github.com/anthropics/claude-code/issues/24052), [#21925](https://github.com/anthropics/claude-code/issues/21925))

Cozempic prevents this with three layers of protection:

1. **Continuous checkpoint** — saves team state to disk every N seconds so it's always recoverable
2. **Hook-driven checkpoint** — fires after every Task spawn, TaskCreate/Update, before compaction, and at session end
3. **Emergency prune** — at a configurable size threshold, prunes dead weight with team-protect, injects recovery messages, and optionally reloads Claude

**Zero external dependencies.** Python 3.10+ stdlib only.

## Install

```bash
pip install cozempic
```

Or run directly:

```bash
git clone https://github.com/Ruya-AI/cozempic.git
cd cozempic
pip install -e .
```

## Quick Start

```bash
# List all sessions with sizes
cozempic list

# Auto-detect and diagnose the current session
cozempic current --diagnose

# Dry-run the standard prescription on current session
cozempic treat current

# Apply with backup
cozempic treat current --execute

# Go aggressive on a specific session
cozempic treat <session_id> -rx aggressive --execute

# Save team/agent state right now (no pruning, instant)
cozempic checkpoint --show

# Keep context clean automatically — run in a separate terminal
cozempic guard --threshold 50 -rx standard

# Treat + auto-resume in a new terminal
cozempic reload -rx gentle
```

Session IDs accept full UUIDs, UUID prefixes, file paths, or `current` for auto-detection based on your working directory.

## How It Works

Each type of bloat has a dedicated **strategy** that knows exactly what to remove and what to keep. Strategies are grouped into **prescriptions** — presets that balance cleaning depth against risk:

| Prescription | Strategies | Risk | Typical Savings |
|---|---|---|---|
| `gentle` | 3 | Minimal | 5-8% |
| `standard` | 7 | Low | 15-20% |
| `aggressive` | 13 | Moderate | 20-25% |

**Dry-run is the default.** Nothing is modified until you pass `--execute`. Backups are always created automatically.

## Strategies

| # | Strategy | What It Does | Expected |
|---|----------|-------------|----------|
| 1 | `progress-collapse` | Collapse consecutive progress tick messages | 40-48% |
| 2 | `file-history-dedup` | Deduplicate file-history-snapshot messages | 3-6% |
| 3 | `metadata-strip` | Strip token usage stats, stop_reason, costs | 1-3% |
| 4 | `thinking-blocks` | Remove/truncate thinking content + signatures | 2-5% |
| 5 | `tool-output-trim` | Trim large tool results (>8KB or >100 lines) | 1-8% |
| 6 | `stale-reads` | Remove file reads superseded by later edits | 0.5-2% |
| 7 | `system-reminder-dedup` | Deduplicate repeated system-reminder tags | 0.1-3% |
| 8 | `http-spam` | Collapse consecutive HTTP request runs | 0-2% |
| 9 | `error-retry-collapse` | Collapse repeated error-retry sequences | 0-5% |
| 10 | `background-poll-collapse` | Collapse repeated polling messages | 0-1% |
| 11 | `document-dedup` | Deduplicate large document blocks | 0-44% |
| 12 | `mega-block-trim` | Trim any content block over 32KB | safety net |
| 13 | `envelope-strip` | Strip constant envelope fields | 2-4% |

Run a single strategy:

```bash
cozempic strategy progress-collapse <session_id> -v
cozempic strategy thinking-blocks <session_id> --thinking-mode truncate
```

## Commands

```
cozempic list [--project NAME]          List sessions with sizes
cozempic current [-d]                   Show/diagnose current session (auto-detect)
cozempic diagnose <session>             Analyze bloat sources (read-only)
cozempic treat <session> [-rx PRESET]   Run prescription (dry-run default)
cozempic treat <session> --execute      Apply changes with backup
cozempic strategy <name> <session>      Run single strategy
cozempic reload [-rx PRESET]            Treat + auto-resume in new terminal
cozempic checkpoint [--show]            Save team/agent state to disk (no pruning)
cozempic guard [--threshold MB]         Continuous checkpoint + emergency prune (background)
cozempic doctor [--fix]                 Check for known Claude Code issues
cozempic formulary                      Show all strategies & prescriptions
```

Use `current` as the session argument in any command to auto-detect the active session for your working directory.

## Checkpoint — Instant Team State Snapshot

Save your current team/agent state to disk without pruning or modifying anything:

```bash
# Save team state
cozempic checkpoint

# Save and print the state
cozempic checkpoint --show
```

Output:

```
  Checkpoint: 6 subagents, 9 tasks -> team-checkpoint.md

Active agent team: agents

Subagents (6):
  - af9763f [Explore] — Explore memory system [completed]
    Result: Complete understanding of the memory system...
  - aa79e90 [Explore] — Explore platform adapters [completed]
    Result: Comprehensive technical summary of platform...
  ...

Shared task list:
  - [COMPLETED] Fix team detection
  - [IN_PROGRESS] Add continuous checkpoint
  - [PENDING] Update README
```

### What gets detected

Cozempic scans the JSONL session file for all team coordination patterns:

| Pattern | Source | What's Extracted |
|---------|--------|-----------------|
| `Task` tool calls | Subagent spawns | agent_id, subagent_type, description, prompt |
| `<task-notification>` | Agent completion messages | status, summary, full result text |
| `TaskCreate` / `TaskUpdate` | Shared todo list | task_id, subject, status, owner |
| `TaskOutput` / `TaskStop` | Background agent management | agent status updates |
| `TeamCreate` / `SendMessage` | Explicit team coordination | team name, teammate roles |

The checkpoint is written to `.claude/projects/<project>/team-checkpoint.md`.

## Guard — Continuous Protection

Guard is a background daemon with two phases:

**Phase 1: Continuous checkpoint** (every interval) — extracts team state and writes to disk. Lightweight read-only scan. Team state is always recoverable even if Claude crashes.

**Phase 2: Emergency prune** (at threshold) — when file size crosses the threshold, prunes dead weight with team-protect, injects recovery messages into the JSONL, and optionally kills + resumes Claude.

```bash
# Standard — run in a separate terminal
cozempic guard

# Custom threshold and interval
cozempic guard --threshold 30 --interval 15 -rx standard

# Without auto-reload (just clean, no restart)
cozempic guard --threshold 50 --no-reload
```

Output:

```
  COZEMPIC GUARD v2
  ===================================================================
  Session:     abc123.jsonl
  Size:        5.4MB
  Threshold:   50.0MB (emergency prune)
  Rx:          standard
  Interval:    30s
  Checkpoint:  continuous (every 30s)

  Guarding... (Ctrl+C to stop)

  [14:23:01] Checkpoint #1: 6 agents, 9 tasks, 121 msgs (5.4MB)
  [14:25:31] Checkpoint #2: 8 agents, 12 tasks, 156 msgs (6.1MB)
  [14:28:01] Checkpoint #3: 8 agents, 12 tasks, 189 msgs (7.2MB)
```

On Ctrl+C, guard writes a final checkpoint before exiting.

### How team-protect works

During emergency prune:

1. **Extract** full team state (subagents, tasks, teammates, coordination history)
2. **Separate** team messages from non-team messages
3. **Prune** only non-team messages using the prescription
4. **Merge** team messages back at their original positions
5. **Inject** a synthetic message pair confirming team state (Claude *sees* this as conversation history)
6. **Save** with backup, then optionally reload

## Hook Integration

For the strongest protection, wire `cozempic checkpoint` into Claude Code hooks. This captures team state at every critical moment — not just on a timer.

Add to your project's `.claude/settings.json`:

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Task",
        "hooks": [
          {
            "type": "command",
            "command": "cozempic checkpoint 2>/dev/null || true"
          }
        ]
      },
      {
        "matcher": "TaskCreate|TaskUpdate",
        "hooks": [
          {
            "type": "command",
            "command": "cozempic checkpoint 2>/dev/null || true"
          }
        ]
      }
    ],
    "PreCompact": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "cozempic checkpoint 2>/dev/null || true"
          }
        ]
      }
    ],
    "Stop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "cozempic checkpoint 2>/dev/null || true"
          }
        ]
      }
    ]
  }
}
```

This checkpoints team state:

| Hook | When | Why |
|------|------|-----|
| `PostToolUse[Task]` | After every subagent spawn | Capture new agent immediately |
| `PostToolUse[TaskCreate\|TaskUpdate]` | After todo list changes | Track task progress |
| `PreCompact` | Right before auto-compaction | Last chance to save state |
| `Stop` | Session end | Final checkpoint |

### Protection layers summary

| Layer | Trigger | What it does |
|-------|---------|-------------|
| **Hooks** | Every Task/TaskCreate/TaskUpdate, PreCompact, Stop | Instant checkpoint to disk |
| **Guard** | Every N seconds | Continuous checkpoint + emergency prune at threshold |
| **Reload** | Manual (`cozempic reload`) | One-shot prune + auto-resume |
| **Checkpoint** | Manual (`cozempic checkpoint`) | One-shot state save |

## Reload — Treat + Auto-Resume

Prune the current session and automatically resume Claude in a new terminal:

```bash
cozempic reload -rx gentle
```

This:
1. Treats the current session with the chosen prescription
2. Generates a compact recap of the conversation
3. Spawns a watcher that waits for Claude to exit
4. When you type `/exit`, a new terminal opens with `claude --resume`
5. The recap is displayed before the resume prompt

## Doctor

Beyond context cleaning, Cozempic can check for known Claude Code configuration issues:

```bash
cozempic doctor        # Diagnose issues
cozempic doctor --fix  # Auto-fix where possible
```

Current checks:

| Check | What It Detects |
|-------|----------------|
| `trust-dialog-hang` | `hasTrustDialogAccepted=true` in `~/.claude.json` causing resume hangs on Windows |
| `oversized-sessions` | Session files >50MB likely to hang on resume |
| `stale-backups` | Old `.bak` files from previous treatments wasting disk |
| `disk-usage` | Total session storage exceeding healthy thresholds |

The `--fix` flag auto-applies fixes where safe (e.g., resetting the trust dialog flag, cleaning stale backups). Backups are created before any config modification.

## Claude Code Integration

### Slash Command

Cozempic ships with a `/cozempic` slash command for Claude Code. Install it by copying the command file to your user-level commands directory:

```bash
cp .claude/commands/cozempic.md ~/.claude/commands/cozempic.md
```

Then from any Claude Code session, type `/cozempic` to diagnose and treat the current session interactively. You can also pass a prescription directly: `/cozempic aggressive`.

After treatment, exit and resume the session to load the pruned context:

```bash
claude --resume
```

### SessionStart Hook (Optional)

To persist the session ID as an environment variable for use in scripts and other hooks:

```bash
cp .claude/hooks/persist-session-id.sh ~/.claude/hooks/
chmod +x ~/.claude/hooks/persist-session-id.sh
```

Add to your `.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [{
      "hooks": [{
        "type": "command",
        "command": "~/.claude/hooks/persist-session-id.sh"
      }]
    }]
  }
}
```

This makes `$CLAUDE_SESSION_ID` available in all Bash commands during the session.

## Safety

- **Always dry-run by default** — `--execute` flag required to modify files
- **Timestamped backups** — automatic `.bak` files before any modification
- **Never touches uuid/parentUuid** — conversation DAG stays intact
- **Never removes summary/queue-operation messages** — structurally important
- **Team messages are protected** — guard and checkpoint never prune Task, TaskCreate, TaskUpdate, TeamCreate, or SendMessage tool calls
- **task-notification results preserved** — agent completion results (the actual output) are captured and checkpointed
- **Strategies compose sequentially** — each runs on the output of the previous, so savings are accurate and don't overlap

## Example Output

```
  Prescription: aggressive
  Before: 29.56MB (6602 messages)
  After:  23.09MB (5073 messages)
  Saved:  6.47MB (21.9%) — 1529 removed, 4038 modified

  Strategy Results:
    progress-collapse              1.63MB saved  (5.5%)  (1525 removed)
    file-history-dedup              2.0KB saved  (0.0%)  (4 removed)
    metadata-strip                693.9KB saved  (2.3%)  (2735 modified)
    thinking-blocks                 1.11MB saved  (3.8%)  (1127 modified)
    tool-output-trim               1.72MB saved  (5.8%)  (167 modified)
    stale-reads                   710.0KB saved  (2.3%)  (176 modified)
    system-reminder-dedup          27.6KB saved  (0.1%)  (92 modified)
    envelope-strip                509.2KB saved  (1.7%)  (4657 modified)
```

## Contributing

Contributions welcome. To add a strategy:

1. Create a function in the appropriate tier file under `src/cozempic/strategies/`
2. Decorate with `@strategy(name, description, tier, expected_savings)`
3. Return a `StrategyResult` with a list of `PruneAction`s
4. Add to the appropriate prescription in `src/cozempic/registry.py`

```python
from cozempic.registry import strategy
from cozempic.types import Message, PruneAction, StrategyResult

@strategy("my-strategy", "What it does", "standard", "1-5%")
def my_strategy(messages: list[Message], config: dict) -> StrategyResult:
    actions = []
    # ... analyze messages, build PruneAction list ...
    return StrategyResult(
        strategy_name="my-strategy",
        actions=actions,
        # ...
    )
```

## License

MIT - see [LICENSE](LICENSE).

Built by [Ruya AI](https://ruya.ai).
