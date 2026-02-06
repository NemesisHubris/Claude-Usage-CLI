# Claude Usage Tracker

Monitor your Claude AI usage limits from the terminal. Single Python file, no dependencies.

## Setup

```bash
# Log into Claude Code first (one-time)
claude login

# Run it
python3 claude_usage.py
```

## Usage

```bash
python3 claude_usage.py                # One-shot check
python3 claude_usage.py --watch        # Live dashboard (refreshes every 60s)
python3 claude_usage.py --watch -i 30  # Custom refresh interval
python3 claude_usage.py --json         # JSON output for scripting
python3 claude_usage.py --no-color     # Disable colors
```

## Watch Mode Commands

| Command | Action |
|---------|--------|
| `/help` | Show commands |
| `/update` | Refresh now |
| `/settings` | Open settings (legend, bar width, extra usage) |
| `/quit` | Exit |

Up/down arrows recall previous commands.

## Requirements

- Python 3.6+
- macOS with `claude login`, or set `CLAUDE_SESSION_KEY` env var
