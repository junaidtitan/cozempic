# Cozempic Plugin for Claude Code

Context weight-loss plugin for Claude Code. Prune bloated sessions, protect agent teams from compaction, and monitor context usage — all from within Claude Code.

## Prerequisites

```bash
pip install cozempic
```

## Install

```bash
# Development / testing
claude --plugin-dir ./plugin

# From marketplace (once published)
claude plugin install cozempic
```

## Skills

| Skill | Description | Invocation |
|-------|-------------|------------|
| `/cozempic:diagnose` | Analyze session bloat, token count, context % | User or Claude (auto) |
| `/cozempic:treat [rx]` | Prune session with gentle/standard/aggressive | User only |
| `/cozempic:reload [rx]` | Treat + auto-resume in new terminal | User only |
| `/cozempic:guard` | Start background sentinel daemon | User only |
| `/cozempic:doctor` | Run health checks | User or Claude (auto) |

## Hooks

Automatically registered when the plugin is enabled:

| Event | Action |
|-------|--------|
| `SessionStart` | Start guard daemon in background |
| `PostToolUse` (Task/TaskCreate/TaskUpdate) | Checkpoint agent team state |
| `PreCompact` | Emergency checkpoint before compaction |
| `Stop` | Final checkpoint on session end |

## MCP Tools

The plugin includes an MCP server that gives Claude direct access to cozempic tools:

- `diagnose_current` — Full session diagnosis with token counts
- `estimate_tokens` — Quick token count + context % check
- `list_sessions` — All sessions with sizes and tokens
- `treat_session` — Dry-run or apply a prescription
- `list_strategies` — Available strategies and prescriptions

Claude can invoke these automatically when it detects context pressure.

## How It Works

The plugin wraps the `cozempic` CLI and Python package:
- **Skills** call `cozempic` CLI commands via Bash
- **Hooks** run `cozempic guard` and `cozempic checkpoint` on lifecycle events
- **MCP server** imports from the `cozempic` package directly for richer tool integration
