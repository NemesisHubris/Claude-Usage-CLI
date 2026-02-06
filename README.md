# Claude Usage Tracker

A single-file Python CLI tool that monitors your Claude AI usage limits in real time. No dependencies beyond the standard library.

```
┌──────────────────────────────────────────────────────────┐
│                   CLAUDE USAGE TRACKER                   │
└──────────────────────────────────────────────────────────┘
  ● System Status: All Systems Operational
  Auth: CLI OAuth  |  Updated: 2026-02-05 14:32:01

  Session Limit (5-Hour Window)
  [████████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░] 28%
  Resets in: 3h 42m  12% under budget

  Weekly Limit
  [██████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░] 15%
  Resets in: 5d 2h 14m  6% under budget
──────────────────────────────────────────────────────────────
  Refreshing in 60s... (Type /help for commands)
>
```

## Quick Start

```bash
# Make sure you're logged into Claude Code first
claude login

# One-shot check
python3 claude_usage.py

# Live dashboard (auto-refreshes every 60s)
python3 claude_usage.py --watch

# Custom refresh interval
python3 claude_usage.py --watch -i 30
```

That's it. If you're logged into Claude Code on macOS, authentication is automatic.

## How It Works

The script reads your Claude Code OAuth credentials directly from the macOS Keychain -- the same ones created when you run `claude login`. No API keys, no browser cookies, no manual setup.

**Authentication methods** (tried in order):

1. **CLI OAuth** -- Auto-read from macOS Keychain (requires `claude login`)
2. **Session Key** -- Set `CLAUDE_SESSION_KEY` environment variable (fallback)

## Usage Limits Tracked

| Limit | Window | Description |
|-------|--------|-------------|
| Session | 5 hours | Rolling usage within current session window |
| Weekly | 7 days | Rolling usage over the past week |
| Opus | 7 days | Per-model limit for Claude Opus |
| Sonnet | 7 days | Per-model limit for Claude Sonnet |
| Cost | Monthly | Overage spend tracking (if enabled) |
| Extra | Monthly | Pay-as-you-go usage (if enabled) |

## Pace Tracking

Each progress bar shows where you *should* be based on elapsed time in the window:

```
  [████████████████████░░░░░░░░░░░░░░░░░░░░] 28%
  Resets in: 3h 42m  12% under budget
```

- **Green** = usage so far
- **Blue** = headroom (how much more you can use and stay on pace)
- **Red** = over budget for this point in the window

If you've used 28% of your limit but only 16% of the time window has passed, you're 12% over budget and the bar shows it in red.

## Watch Mode

Run with `--watch` to get a live dashboard that auto-refreshes. Type commands at the `>` prompt:

| Command | Action |
|---------|--------|
| `/help` | Show available commands |
| `/update` | Force an immediate refresh |
| `/settings` | Open the settings menu |
| `/quit` | Exit (also `/exit` or Ctrl+C) |

Command history is saved between sessions -- use up/down arrows to recall previous commands.

### Settings Menu

Type `/settings` to open an interactive menu:

```
  Settings
  ─────────────────────────────
  [1] Legend        OFF
  [2] Extra Usage   OFF
  [3] Expand bars   (width: 40, +5)
  [4] Shrink bars   (width: 40, -5)
  ─────────────────────────────
  Enter number or press Enter to close
```

All settings persist to disk at `~/.config/claude-usage-tracker/settings.json`.

## All Options

```
python3 claude_usage.py [OPTIONS]

Options:
  --watch, -w          Live dashboard with auto-refresh
  --interval, -i SEC   Refresh interval in seconds (default: 60)
  --json               Output raw JSON (for scripting/piping)
  --no-color           Disable colored output
  --help               Show help
```

## JSON Mode

Pipe usage data into other tools:

```bash
# Pretty-print
python3 claude_usage.py --json

# Extract session percentage with jq
python3 claude_usage.py --json | jq '.session.percentage'

# Alert if session usage exceeds 80%
python3 claude_usage.py --json | jq -e '.session.percentage > 80' && echo "WARNING: High usage"
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `CLAUDE_SESSION_KEY` | Session key for cookie-based auth (fallback) |
| `NO_COLOR` | Disable colors (respects [no-color.org](https://no-color.org) convention) |
| `FORCE_COLOR` | Force colors even when not a TTY |
| `XDG_CONFIG_HOME` | Override config directory (default: `~/.config`) |

## Requirements

- Python 3.6+
- macOS (for Keychain auth) or any platform with `CLAUDE_SESSION_KEY` set
- No external dependencies

## Troubleshooting

**"No valid auth method"**
Run `claude login` in your terminal first. The script reads the same credentials Claude Code stores in your Keychain.

**"CLI OAuth: token expired"**
Your token has expired. Run `claude login` again to refresh it.

**"Session key: blocked by Cloudflare"**
Session key auth is unreliable due to Cloudflare bot detection. Use CLI OAuth instead (`claude login`).

**Colors look wrong**
Try `--no-color`, or set `FORCE_COLOR=1` if colors should work but aren't detected.

**"Refresh failed" in watch mode**
Network errors trigger automatic retries with exponential backoff (up to 5 minutes). The dashboard will recover on its own.
