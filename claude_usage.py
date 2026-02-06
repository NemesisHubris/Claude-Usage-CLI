#!/usr/bin/env python3
"""
Claude Usage Tracker - CLI Edition
A cross-platform command-line tool to monitor Claude AI usage limits.
Faithful port of the Swift macOS app with full auth fallback.

Usage:
  python3 claude_usage.py                  # One-shot, interactive
  python3 claude_usage.py --watch          # Auto-refresh every 60s
  python3 claude_usage.py --watch -i 30    # Auto-refresh every 30s
  python3 claude_usage.py --json           # JSON output (for scripting)
  python3 claude_usage.py --no-color       # Disable colors
  python3 claude_usage.py --help           # Show help
"""

import urllib.request
import urllib.error
import json
import sys
import os
import subprocess
import time
import argparse
import platform
import threading
import queue
try:
    import readline  # Enable arrow keys/history support
    HAS_READLINE = True
except ImportError:
    readline = None  # type: ignore[assignment]
    HAS_READLINE = False
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List


# ==========================================
# Settings (persistent)
# ==========================================

_CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    "claude-usage-tracker",
)

SETTINGS_PATH = os.path.join(_CONFIG_DIR, "settings.json")
HISTORY_PATH = os.path.join(_CONFIG_DIR, "history")

# Bar width limits
BAR_WIDTH_MIN = 20
BAR_WIDTH_MAX = 80
BAR_WIDTH_STEP = 5
BAR_WIDTH_DEFAULT = 40


class Settings:
    def __init__(self):
        self.show_legend: bool = False
        self.show_extra: bool = False
        self.bar_width: int = BAR_WIDTH_DEFAULT
        self._load()

    def _load(self):
        """Load settings from disk. Missing keys keep defaults."""
        try:
            with open(SETTINGS_PATH, "r") as f:
                data = json.load(f)
            if isinstance(data.get("show_legend"), bool):
                self.show_legend = data["show_legend"]
            if isinstance(data.get("show_extra"), bool):
                self.show_extra = data["show_extra"]
            if isinstance(data.get("bar_width"), int):
                self.bar_width = max(BAR_WIDTH_MIN, min(BAR_WIDTH_MAX, data["bar_width"]))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass

    def save(self):
        """Persist current settings to disk."""
        try:
            os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
            with open(SETTINGS_PATH, "w") as f:
                json.dump({
                    "show_legend": self.show_legend,
                    "show_extra": self.show_extra,
                    "bar_width": self.bar_width,
                }, f, indent=2)
        except OSError:
            pass

    def expand(self):
        self.bar_width = min(BAR_WIDTH_MAX, self.bar_width + BAR_WIDTH_STEP)
        self.save()

    def shrink(self):
        self.bar_width = max(BAR_WIDTH_MIN, self.bar_width - BAR_WIDTH_STEP)
        self.save()


# Global settings
SETTINGS = Settings()


# ==========================================
# Cross-platform color support
# ==========================================

class Colors:
    """ANSI color codes with auto-detection for terminal support."""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled and self._supports_color()

    @staticmethod
    def _supports_color() -> bool:
        if os.environ.get("NO_COLOR"):
            return False
        if os.environ.get("FORCE_COLOR"):
            return True
        if not hasattr(sys.stdout, "isatty") or not sys.stdout.isatty():
            return False
        if platform.system() == "Windows":
            if os.environ.get("TERM") == "xterm":
                return True
            try:
                return int(platform.version().split(".")[0]) >= 10
            except (ValueError, IndexError):
                return False
        return True

    def _code(self, code: str) -> str:
        return code if self.enabled else ""

    # Styles
    @property
    def BOLD(self): return self._code("\033[1m")
    @property
    def DIM(self): return self._code("\033[2m")
    @property
    def RESET(self): return self._code("\033[0m")
    @property
    def REVERSE(self): return self._code("\033[7m")

    # Colors
    @property
    def RED(self): return self._code("\033[91m")
    @property
    def GREEN(self): return self._code("\033[92m")
    @property
    def YELLOW(self): return self._code("\033[93m")
    @property
    def BLUE(self): return self._code("\033[94m")
    @property
    def MAGENTA(self): return self._code("\033[95m")
    @property
    def CYAN(self): return self._code("\033[96m")
    @property
    def WHITE(self): return self._code("\033[97m")
    @property
    def GRAY(self): return self._code("\033[90m")

    def cursor_up(self, n: int) -> str:
        return f"\033[{n}A" if self.enabled else ""

    def clear_to_end(self) -> str:
        return "\033[0J" if self.enabled else ""

    def clear_line(self) -> str:
        return "\033[2K" if self.enabled else ""

    def hide_cursor(self) -> str:
        return "\033[?25l" if self.enabled else ""

    def show_cursor(self) -> str:
        return "\033[?25h" if self.enabled else ""


# Global instance, configured later
C = Colors()


# ==========================================
# Errors
# ==========================================

class AppError(Exception):
    def __init__(self, message, code=None):
        self.message = message
        self.code = code
        super().__init__(message)


# ==========================================
# Auth: macOS Keychain (CLI OAuth)
# ==========================================

def read_cli_credentials_from_keychain() -> Optional[str]:
    """
    Reads Claude Code OAuth credentials from macOS Keychain.
    Mirrors ClaudeCodeSyncService.readSystemCredentials()
    """
    if platform.system() != "Darwin":
        return None
    try:
        result = subprocess.run(
            ["/usr/bin/security", "find-generic-password",
             "-s", "Claude Code-credentials",
             "-a", os.environ.get("USER", ""),
             "-w"],
            capture_output=True, text=True, timeout=10
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except Exception:
        return None


def extract_access_token(json_data: str) -> Optional[str]:
    try:
        return json.loads(json_data).get("claudeAiOauth", {}).get("accessToken")
    except Exception:
        return None


def is_token_expired(json_data: str) -> bool:
    try:
        exp = json.loads(json_data).get("claudeAiOauth", {}).get("expiresAt")
        return time.time() > float(exp) if exp else False
    except Exception:
        return False


# ==========================================
# ClaudeAPIService
# ==========================================

class ClaudeAPIService:
    CLAUDE_BASE = "https://claude.ai/api"
    OAUTH_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
    USER_AGENT = "claude-code/2.1.5"

    def __init__(self, session_key: Optional[str] = None):
        self.session_key = None
        self.oauth_token = None
        self.auth_method = None

        if session_key:
            sk = session_key.strip()
            if sk.startswith("sessionKey="):
                sk = sk[len("sessionKey="):]
            if sk:
                self.session_key = sk

    def _request_session(self, url: str) -> Any:
        headers = {"Cookie": f"sessionKey={self.session_key}", "Accept": "application/json"}
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _request_oauth(self, url: str) -> Any:
        headers = {
            "Authorization": f"Bearer {self.oauth_token}",
            "Content-Type": "application/json",
            "User-Agent": self.USER_AGENT,
            "anthropic-beta": "oauth-2025-04-20",
        }
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise AppError("Authentication failed (401). Please try running 'claude login' to refresh your credentials.", code=401)
            elif e.code == 403:
                raise AppError("Access denied (403). Your token may be invalid.", code=403)
            raise e

    def authenticate(self, quiet: bool = False):
        # 1. Try session key
        if self.session_key:
            try:
                self._request_session(f"{self.CLAUDE_BASE}/organizations")
                self.auth_method = "session"
                return
            except Exception:
                if not quiet:
                    print(f"{C.YELLOW}Session key: blocked by Cloudflare{C.RESET}")

        # 2. Try CLI OAuth
        creds = read_cli_credentials_from_keychain()
        if creds:
            if is_token_expired(creds):
                if not quiet:
                    print(f"{C.YELLOW}CLI OAuth: token expired{C.RESET}")
            else:
                token = extract_access_token(creds)
                if token:
                    self.oauth_token = token.strip()
                    self.auth_method = "oauth"

                    # Verify token immediately to fail fast
                    try:
                        self.fetch_usage()
                    except AppError as e:
                        if e.code in (401, 403):
                            if not quiet:
                                print(f"{C.YELLOW}CLI OAuth: token rejected by API ({e.code}){C.RESET}")
                            self.oauth_token = None
                            self.auth_method = None
                        else:
                            return
                    except Exception:
                         return
                    else:
                        return

        raise AppError(
            "No valid auth method.\n"
            "  Fix: Run 'claude login' first, then re-run this script.",
            code=403
        )

    def fetch_organizations(self) -> List[Dict[str, Any]]:
        if self.auth_method == "oauth" and self.session_key:
            try:
                resp = self._request_session(f"{self.CLAUDE_BASE}/organizations")
                if isinstance(resp, list):
                    return resp
            except Exception:
                pass
            return []
        if self.auth_method == "oauth":
            return []
        resp = self._request_session(f"{self.CLAUDE_BASE}/organizations")
        return resp if isinstance(resp, list) else []

    def fetch_usage(self, org_id: Optional[str] = None) -> Dict[str, Any]:
        if self.auth_method == "oauth":
            return self._request_oauth(self.OAUTH_USAGE_URL)

        if not org_id:
            raise AppError("Organization ID required for session key auth")
        data = self._request_session(f"{self.CLAUDE_BASE}/organizations/{org_id}/usage")
        try:
            overage = self._request_session(f"{self.CLAUDE_BASE}/organizations/{org_id}/overage_spend_limit")
            if overage and overage.get("is_enabled"):
                data["cost_data"] = overage
        except Exception:
            pass
        return data


# ==========================================
# Parsing
# ==========================================

def parse_util(val) -> float:
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        try:
            return float(val.strip().replace("%", ""))
        except ValueError:
            return 0.0
    return 0.0


def parse_usage(data: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(data, dict):
        return {"session": {}, "weekly": {}, "opus": {}, "sonnet": {}, "cost": {}, "extra": {}}

    parsed: Dict[str, Any] = {"session": {}, "weekly": {}, "opus": {}, "sonnet": {}, "cost": {}, "extra": {}}

    if data.get("five_hour"):
        fh = data["five_hour"]
        parsed["session"]["percentage"] = parse_util(fh.get("utilization", 0))
        parsed["session"]["reset_at"] = fh.get("resets_at")

    if data.get("seven_day"):
        sd = data["seven_day"]
        parsed["weekly"]["percentage"] = parse_util(sd.get("utilization", 0))
        parsed["weekly"]["reset_at"] = sd.get("resets_at")

    if data.get("seven_day_opus"):
        parsed["opus"]["percentage"] = parse_util(data["seven_day_opus"].get("utilization", 0))
        parsed["opus"]["reset_at"] = data["seven_day_opus"].get("resets_at")

    if data.get("seven_day_sonnet"):
        parsed["sonnet"]["percentage"] = parse_util(data["seven_day_sonnet"].get("utilization", 0))
        parsed["sonnet"]["reset_at"] = data["seven_day_sonnet"].get("resets_at")

    if data.get("cost_data"):
        c = data["cost_data"]
        parsed["cost"]["used"] = c.get("used_credits", 0)
        parsed["cost"]["limit"] = c.get("monthly_credit_limit", 0)
        parsed["cost"]["currency"] = c.get("currency", "USD")

    if data.get("extra_usage"):
        eu = data["extra_usage"]
        parsed["extra"]["enabled"] = eu.get("is_enabled", False)
        parsed["extra"]["utilization"] = parse_util(eu.get("utilization", 0))
        parsed["extra"]["used"] = eu.get("used_credits", 0)
        parsed["extra"]["limit"] = eu.get("monthly_limit", 0)

    return parsed


# ==========================================
# Time helpers
# ==========================================

def parse_iso_to_datetime(iso_str: str) -> Optional[datetime]:
    """Parse an ISO datetime string to a timezone-aware datetime object."""
    try:
        clean = iso_str.replace("+00:00", "+0000").replace("Z", "+0000")
        if "." in clean:
            dot = clean.index(".")
            sign_pos = max(clean.rfind("+", dot), clean.rfind("-", dot))
            if sign_pos > dot:
                clean = clean[:dot] + clean[sign_pos:]
            else:
                clean = clean[:dot]
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
            try:
                target = datetime.strptime(clean, fmt)
                if target.tzinfo is None:
                    target = target.replace(tzinfo=timezone.utc)
                return target
            except ValueError:
                continue
    except Exception:
        pass
    return None


def time_until(iso_str: Optional[str]) -> str:
    """Returns human-readable countdown like '6d 2h 14m' or 'now'."""
    if not iso_str:
        return "unknown"
    target = parse_iso_to_datetime(iso_str)
    if not target:
        return iso_str

    now = datetime.now(timezone.utc)
    diff = (target - now).total_seconds()

    if diff <= 0:
        return "now"

    days = int(diff // 86400)
    hours = int((diff % 86400) // 3600)
    minutes = int((diff % 3600) // 60)

    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0 or days > 0:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")

    return " ".join(parts)


def calc_time_elapsed_pct(reset_at_iso: Optional[str], window_hours: float) -> Optional[float]:
    """
    Calculate what % of the time window has elapsed.
    reset_at is when the window ENDS. window_hours is the total window duration.
    """
    if not reset_at_iso:
        return None
    target = parse_iso_to_datetime(reset_at_iso)
    if not target:
        return None

    now = datetime.now(timezone.utc)
    window_start = target - timedelta(hours=window_hours)
    total_secs = window_hours * 3600
    elapsed_secs = (now - window_start).total_seconds()

    if elapsed_secs < 0:
        return 0.0
    if elapsed_secs > total_secs:
        return 100.0
    return (elapsed_secs / total_secs) * 100.0


# ==========================================
# Display
# ==========================================

BLOCK = "\u2588"
SHADE = "\u2591"


def fetch_system_status() -> Optional[Dict[str, str]]:
    """Fetches Claude system status from Anthropic's status page."""
    try:
        url = "https://status.anthropic.com/api/v2/status.json"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return {
                "indicator": data.get("status", {}).get("indicator", "none"),
                "description": data.get("status", {}).get("description", "Unknown")
            }
    except Exception:
        return None


def usage_bar(pct: float, width: int = BAR_WIDTH_DEFAULT) -> str:
    """Colored progress bar."""
    pct = max(0.0, min(100.0, pct))
    filled = int(pct / 100 * width)
    filled = max(0, min(width, filled))

    if pct >= 90:
        color = C.RED
    elif pct >= 70:
        color = C.YELLOW
    else:
        color = C.GREEN

    bar = color + BLOCK * filled + C.RESET + C.DIM + SHADE * (width - filled) + C.RESET
    return bar


def pace_bar(used_pct: float, budget_pct: Optional[float], width: int = BAR_WIDTH_DEFAULT) -> tuple:
    """
    Multi-color progress bar with pace tracking.

    Colors:
      GREEN  = amount used so far (when under or at budget)
      BLUE   = headroom / how much more you can spend to stay on track
      RED    = amount OVER what you should have used by now

    Returns (bar_string, pace_description_string)
    """
    used_pct = max(0.0, min(100.0, used_pct))

    if budget_pct is None:
        filled = int(used_pct / 100 * width)
        filled = max(0, min(width, filled))
        if used_pct >= 90:
            color = C.RED
        elif used_pct >= 70:
            color = C.YELLOW
        else:
            color = C.GREEN
        bar = color + BLOCK * filled + C.RESET + C.DIM + SHADE * (width - filled) + C.RESET
        return bar, ""

    budget_pct = max(0.0, min(100.0, budget_pct))

    if used_pct <= budget_pct:
        green_cells = int(used_pct / 100 * width)
        budget_cells = int(budget_pct / 100 * width)
        blue_cells = budget_cells - green_cells
        empty_cells = width - budget_cells

        green_cells = max(0, green_cells)
        blue_cells = max(0, blue_cells)
        empty_cells = max(0, empty_cells)

        total = green_cells + blue_cells + empty_cells
        if total < width:
            empty_cells += (width - total)
        elif total > width:
            empty_cells -= (total - width)
            if empty_cells < 0:
                blue_cells += empty_cells
                empty_cells = 0

        bar = (C.GREEN + BLOCK * green_cells +
               C.BLUE + BLOCK * blue_cells + C.RESET +
               C.DIM + SHADE * max(0, empty_cells) + C.RESET)

        delta = budget_pct - used_pct
        pace_str = f"{C.GREEN}{delta:.0f}% under budget{C.RESET}"
    else:
        budget_cells = int(budget_pct / 100 * width)
        used_cells = int(used_pct / 100 * width)
        green_cells = budget_cells
        red_cells = used_cells - budget_cells
        empty_cells = width - used_cells

        green_cells = max(0, green_cells)
        red_cells = max(0, red_cells)
        empty_cells = max(0, empty_cells)

        total = green_cells + red_cells + empty_cells
        if total < width:
            empty_cells += (width - total)
        elif total > width:
            empty_cells -= (total - width)
            if empty_cells < 0:
                red_cells += empty_cells
                empty_cells = 0

        bar = (C.GREEN + BLOCK * green_cells +
               C.RED + BLOCK * red_cells + C.RESET +
               C.DIM + SHADE * max(0, empty_cells) + C.RESET)

        delta = used_pct - budget_pct
        if delta > 20:
            pace_str = f"{C.RED}{C.BOLD}{delta:.0f}% OVER budget!{C.RESET}"
        else:
            pace_str = f"{C.RED}{delta:.0f}% over budget{C.RESET}"

    return bar, pace_str


def generate_report(usage: Dict[str, Any], auth_method: str, system_status: Optional[Dict[str, str]] = None) -> List[str]:
    lines = []
    bw = SETTINGS.bar_width
    # Box width adapts to bar width: bar + brackets + percentage + padding
    w = max(60, bw + 20)

    lines.append("")
    lines.append(f"{C.BOLD}{C.CYAN}\u250c{'─' * (w - 2)}\u2510{C.RESET}")
    lines.append(f"{C.BOLD}{C.CYAN}\u2502{'CLAUDE USAGE TRACKER':^{w - 2}}\u2502{C.RESET}")
    lines.append(f"{C.BOLD}{C.CYAN}\u2514{'─' * (w - 2)}\u2518{C.RESET}")

    # System Status
    if system_status:
        ind = system_status.get("indicator", "none")
        desc = system_status.get("description", "Unknown")

        if ind == "none":
            icon = f"{C.GREEN}●{C.RESET}"
        elif ind == "minor":
            icon = f"{C.YELLOW}●{C.RESET}"
        elif ind in ("major", "critical"):
            icon = f"{C.RED}●{C.RESET}"
        else:
            icon = f"{C.GREEN}●{C.RESET}"

        lines.append(f"  {icon} {C.BOLD}System Status:{C.RESET} {desc}")

    # Auth info
    auth_label = "Session Key" if auth_method == "session" else "CLI OAuth"
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines.append(f"  {C.DIM}Auth: {auth_label}  |  Updated: {now_str}{C.RESET}")

    # Legend (Optional)
    if SETTINGS.show_legend:
        lines.append("")
        lines.append(f"  {C.DIM}Legend: {C.RESET}{C.GREEN}{BLOCK}{BLOCK}{C.RESET}{C.DIM}=used  "
                     f"{C.RESET}{C.BLUE}{BLOCK}{BLOCK}{C.RESET}{C.DIM}=can spend  "
                     f"{C.RESET}{C.RED}{BLOCK}{BLOCK}{C.RESET}{C.DIM}=over budget  "
                     f"{C.RESET}{C.DIM}{SHADE}{SHADE}=remaining{C.RESET}")

    # Session usage (5-hour window)
    sess = usage.get("session", {})
    if sess:
        pct = sess.get("percentage", 0)
        reset = time_until(sess.get("reset_at"))
        budget = calc_time_elapsed_pct(sess.get("reset_at"), 5.0)
        bar, pace = pace_bar(pct, budget, bw)
        lines.append("")
        lines.append(f"  {C.BOLD}Session Limit (5-Hour Window){C.RESET}")
        lines.append(f"  [{bar}] {C.BOLD}{pct:.0f}%{C.RESET}")

        info = f"  {C.DIM}Resets in: {reset}{C.RESET}"
        if pace:
            info += f"  {pace}"
        lines.append(info)

    # Weekly usage (7-day / 168-hour window)
    weekly = usage.get("weekly", {})
    if weekly:
        pct = weekly.get("percentage", 0)
        reset = time_until(weekly.get("reset_at"))
        budget = calc_time_elapsed_pct(weekly.get("reset_at"), 168.0)
        bar, pace = pace_bar(pct, budget, bw)
        lines.append("")
        lines.append(f"  {C.BOLD}Weekly Limit{C.RESET}")
        lines.append(f"  [{bar}] {C.BOLD}{pct:.0f}%{C.RESET}")

        info = f"  {C.DIM}Resets in: {reset}{C.RESET}"
        if pace:
            info += f"  {pace}"
        lines.append(info)

    # Opus
    opus = usage.get("opus", {})
    if opus.get("percentage", 0) > 0:
        pct = opus["percentage"]
        reset = time_until(opus.get("reset_at"))
        budget = calc_time_elapsed_pct(opus.get("reset_at"), 168.0)
        bar, pace = pace_bar(pct, budget, bw)
        lines.append("")
        lines.append(f"  {C.BOLD}{C.MAGENTA}Opus Limit{C.RESET}")
        lines.append(f"  [{bar}] {C.BOLD}{pct:.0f}%{C.RESET}")
        info = ""
        if reset != "unknown":
            info += f"  {C.DIM}Resets in: {reset}{C.RESET}"
        if pace:
            info += f"  {pace}"
        if info:
            lines.append(info)

    # Sonnet
    sonnet = usage.get("sonnet", {})
    if sonnet.get("percentage", 0) > 0:
        pct = sonnet["percentage"]
        reset = time_until(sonnet.get("reset_at"))
        budget = calc_time_elapsed_pct(sonnet.get("reset_at"), 168.0)
        bar, pace = pace_bar(pct, budget, bw)
        lines.append("")
        lines.append(f"  {C.BOLD}{C.BLUE}Sonnet Limit{C.RESET}")
        lines.append(f"  [{bar}] {C.BOLD}{pct:.0f}%{C.RESET}")
        info = ""
        if reset != "unknown":
            info += f"  {C.DIM}Resets in: {reset}{C.RESET}"
        if pace:
            info += f"  {pace}"
        if info:
            lines.append(info)

    # Cost
    cost = usage.get("cost", {})
    if cost.get("used") or cost.get("limit"):
        currency = cost.get("currency", "USD")
        used = cost.get("used", 0)
        limit = cost.get("limit", 0)
        lines.append("")
        lines.append(f"  {C.BOLD}Cost / Overage{C.RESET}")
        lines.append(f"  Used:  {currency} {used}")
        lines.append(f"  Limit: {currency} {limit}")

    # Extra Usage (Optional)
    extra = usage.get("extra", {})
    if SETTINGS.show_extra and extra.get("enabled"):
        lines.append("")
        lines.append(f"  {C.BOLD}Extra Usage (Pay-as-you-go){C.RESET}")
        pct = extra.get("utilization", 0)
        bar = usage_bar(pct, bw)
        lines.append(f"  [{bar}] {C.BOLD}{pct:.0f}%{C.RESET}")
        lines.append(f"  Used: {extra.get('used', 0)} / {extra.get('limit', 0)}")

    lines.append("")
    lines.append(f"{C.CYAN}{'─' * w}{C.RESET}")
    return lines


# ==========================================
# Interactive settings menu
# ==========================================

def show_settings_menu(input_queue: queue.Queue) -> bool:
    """
    Show an interactive numbered settings menu.
    Returns True if a setting was changed (needs redraw).
    Drains the input_queue for the user's selection.
    """
    menu_lines = [
        "",
        f"  {C.BOLD}{C.CYAN}Settings{C.RESET}",
        f"  {C.CYAN}─────────────────────────────{C.RESET}",
    ]

    legend_state = f"{C.GREEN}ON{C.RESET}" if SETTINGS.show_legend else f"{C.DIM}OFF{C.RESET}"
    extra_state = f"{C.GREEN}ON{C.RESET}" if SETTINGS.show_extra else f"{C.DIM}OFF{C.RESET}"
    bw_label = f"{C.BOLD}{SETTINGS.bar_width}{C.RESET}"

    menu_lines.append(f"  {C.CYAN}[1]{C.RESET} Legend        {legend_state}")
    menu_lines.append(f"  {C.CYAN}[2]{C.RESET} Extra Usage   {extra_state}")
    menu_lines.append(f"  {C.CYAN}[3]{C.RESET} Expand bars   {C.DIM}(width: {bw_label}{C.DIM}, +{BAR_WIDTH_STEP}){C.RESET}")
    menu_lines.append(f"  {C.CYAN}[4]{C.RESET} Shrink bars   {C.DIM}(width: {bw_label}{C.DIM}, -{BAR_WIDTH_STEP}){C.RESET}")
    menu_lines.append(f"  {C.CYAN}─────────────────────────────{C.RESET}")
    menu_lines.append(f"  {C.DIM}Enter number or press Enter to close{C.RESET}")
    menu_lines.append("")

    for ml in menu_lines:
        print(ml)

    # Wait for user input (blocking -- we read from the queue which is fed by the input thread)
    while True:
        try:
            choice = input_queue.get(timeout=30)
        except queue.Empty:
            # Timeout, close menu
            return False

        choice = choice.strip()
        if choice == "1":
            SETTINGS.show_legend = not SETTINGS.show_legend
            SETTINGS.save()
            state = f"{C.GREEN}ON{C.RESET}" if SETTINGS.show_legend else f"{C.DIM}OFF{C.RESET}"
            print(f"  {C.DIM}Legend:{C.RESET} {state}")
            return True
        elif choice == "2":
            SETTINGS.show_extra = not SETTINGS.show_extra
            SETTINGS.save()
            state = f"{C.GREEN}ON{C.RESET}" if SETTINGS.show_extra else f"{C.DIM}OFF{C.RESET}"
            print(f"  {C.DIM}Extra Usage:{C.RESET} {state}")
            return True
        elif choice == "3":
            old = SETTINGS.bar_width
            SETTINGS.expand()
            if SETTINGS.bar_width == old:
                print(f"  {C.YELLOW}Already at max width ({BAR_WIDTH_MAX}){C.RESET}")
            else:
                print(f"  {C.DIM}Bar width:{C.RESET} {SETTINGS.bar_width}")
            return True
        elif choice == "4":
            old = SETTINGS.bar_width
            SETTINGS.shrink()
            if SETTINGS.bar_width == old:
                print(f"  {C.YELLOW}Already at min width ({BAR_WIDTH_MIN}){C.RESET}")
            else:
                print(f"  {C.DIM}Bar width:{C.RESET} {SETTINGS.bar_width}")
            return True
        else:
            # Empty enter or anything else closes the menu
            return False


# ==========================================
# Main
# ==========================================

def main():
    parser = argparse.ArgumentParser(
        description="Claude Usage Tracker - monitor your Claude AI usage limits from the command line.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Auth methods (tried in order):
  1. Session key (CLAUDE_SESSION_KEY env var, if set)
  2. CLI OAuth token (auto-read from macOS Keychain if logged into Claude Code)

Examples:
  %(prog)s                      One-shot usage check
  %(prog)s --watch              Auto-refresh every 60s
  %(prog)s --watch -i 30        Auto-refresh every 30s
  %(prog)s --json               JSON output for scripting
  %(prog)s --no-color           Disable colored output
        """
    )
    parser.add_argument("--watch", "-w", action="store_true", help="Auto-refresh mode")
    parser.add_argument("--interval", "-i", type=int, default=60, help="Refresh interval in seconds (default: 60)")
    parser.add_argument("--json", action="store_true", help="Output raw JSON (for scripting)")
    parser.add_argument("--no-color", action="store_true", help="Disable colored output")
    args = parser.parse_args()

    # Configure colors
    global C
    C = Colors(enabled=not args.no_color)

    # Input handling
    input_queue: queue.Queue = queue.Queue()
    input_ready = threading.Event()

    def show_prompt():
        """Print the command prompt. Called by the main thread after every output."""
        sys.stdout.write(f"{C.CYAN}> {C.RESET}")
        sys.stdout.flush()

    def load_history():
        """Load readline command history from disk."""
        if not HAS_READLINE or readline is None:
            return
        try:
            os.makedirs(_CONFIG_DIR, exist_ok=True)
            readline.set_history_length(100)
            if os.path.exists(HISTORY_PATH):
                readline.read_history_file(HISTORY_PATH)
        except OSError:
            pass

    def save_history():
        """Save readline command history to disk."""
        if not HAS_READLINE or readline is None:
            return
        try:
            os.makedirs(_CONFIG_DIR, exist_ok=True)
            readline.write_history_file(HISTORY_PATH)
        except OSError:
            pass

    def input_thread():
        # Wait for initial report to render before reading input
        input_ready.wait()
        while True:
            try:
                # Use input() with empty prompt so readline handles line editing
                # (up/down arrows, backspace, etc.) -- the visible "> " prompt
                # is managed by the main thread via show_prompt()
                cmd = input("")
                input_queue.put(cmd)
            except (EOFError, KeyboardInterrupt):
                input_queue.put("/quit")
                break

    # Start input thread only if watching
    if args.watch:
        load_history()
        t = threading.Thread(target=input_thread, daemon=True)
        t.start()

    # Get session key
    session_key = os.environ.get("CLAUDE_SESSION_KEY")
    service = ClaudeAPIService(session_key=session_key)

    try:
        # Auth
        service.authenticate(quiet=args.json)

        # Org selection (session key auth only, one-time)
        org_id = None
        if service.auth_method == "session":
            orgs = service.fetch_organizations()
            if not orgs:
                raise AppError("No organizations found.")

            if not args.json:
                print(f"\n{C.BOLD}Organizations:{C.RESET}")
                for idx, org in enumerate(orgs, 1):
                    print(f"  {C.CYAN}[{idx}]{C.RESET} {org.get('name', '?')} {C.DIM}({org.get('uuid', '')}){C.RESET}")

                sel = input(f"\n{C.DIM}Select organization [{C.RESET}1{C.DIM}]:{C.RESET} ").strip()
                si = 1
                if sel:
                    try:
                        si = int(sel)
                    except ValueError:
                        si = 1
                si = max(1, min(len(orgs), si))
            else:
                si = 1

            org_id = orgs[si - 1].get("uuid")
            if not org_id:
                raise AppError("Selected organization has no UUID")

            if not args.json:
                print(f"{C.GREEN}Selected: {orgs[si - 1].get('name')}{C.RESET}")

        # Fetch & display loop
        first_run = True
        consecutive_failures = 0
        MAX_BACKOFF = 300

        # Cache last fetched data so /settings redraw doesn't need a re-fetch
        cached_parsed = None
        cached_sys_status = None

        while True:
            try:
                cached_sys_status = fetch_system_status()
                raw = service.fetch_usage(org_id=org_id)
                cached_parsed = parse_usage(raw)
                consecutive_failures = 0

                if args.json:
                    print(json.dumps(cached_parsed, indent=2, default=str))
                    break

                # Clear screen and redraw from top -- eliminates all doubling/glitch bugs
                if args.watch and not first_run:
                    os.system("cls" if platform.system() == "Windows" else "clear")

                report_lines = generate_report(cached_parsed, service.auth_method or "unknown", cached_sys_status)
                for line in report_lines:
                    print(line)

                if args.watch:
                    remaining = args.interval

                    # Show countdown status
                    print(f"  {C.DIM}Refreshing in {remaining}s... (Type /help for commands){C.RESET}")

                    # Signal input thread to start reading + show prompt
                    if first_run:
                        input_ready.set()
                    show_prompt()

                    while remaining > 0:
                        try:
                            cmd = input_queue.get(timeout=1)
                            cmd = cmd.strip().lower()

                            if cmd == "/update":
                                break  # break inner loop -> re-fetch

                            elif cmd == "/settings":
                                changed = show_settings_menu(input_queue)
                                if changed and cached_parsed:
                                    # Redraw with new settings without re-fetching
                                    os.system("cls" if platform.system() == "Windows" else "clear")
                                    report_lines = generate_report(cached_parsed, service.auth_method or "unknown", cached_sys_status)
                                    for line in report_lines:
                                        print(line)
                                    print(f"  {C.DIM}Refreshing in {remaining}s... (Type /help for commands){C.RESET}")
                                show_prompt()

                            elif cmd == "/help":
                                print()
                                print(f"  {C.BOLD}Commands:{C.RESET}")
                                print(f"  {C.CYAN}/update{C.RESET}      Force refresh now")
                                print(f"  {C.CYAN}/settings{C.RESET}    Open settings menu (legend, extra, bar size)")
                                print(f"  {C.CYAN}/quit{C.RESET}        Exit")
                                print()
                                show_prompt()

                            elif cmd in ("/quit", "/exit"):
                                raise KeyboardInterrupt

                            elif cmd:
                                print(f"  {C.RED}Unknown command. Type /help for options.{C.RESET}")
                                show_prompt()

                            else:
                                # Empty enter -- just reshow prompt
                                show_prompt()

                        except queue.Empty:
                            remaining -= 1

                    first_run = False
                else:
                    break

            except KeyboardInterrupt:
                save_history()
                print(f"\n{C.DIM}Stopped.{C.RESET}")
                break
            except AppError:
                raise
            except Exception as e:
                if args.watch:
                    consecutive_failures += 1
                    backoff = min(args.interval * (2 ** (consecutive_failures - 1)), MAX_BACKOFF)
                    print(f"\n{C.RED}Refresh failed: {e}{C.RESET}")
                    print(f"{C.DIM}Retrying in {backoff}s (attempt {consecutive_failures})...{C.RESET}")
                    time.sleep(backoff)
                else:
                    raise AppError(f"Failed to fetch usage: {e}")

    except AppError as e:
        if args.json:
            print(json.dumps({"error": e.message}, indent=2))
        else:
            print(f"\n{C.RED}ERROR: {e.message}{C.RESET}")
        sys.exit(1)
    except KeyboardInterrupt:
        save_history()
        print(f"\n{C.DIM}Stopped.{C.RESET}")
        sys.exit(0)


if __name__ == "__main__":
    main()
