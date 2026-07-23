#!/usr/bin/env python3
import sys
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
"""
manage.py - CLI tool for managing voice-agent project configurations.

Usage:
    python manage.py start               # Interactive: pick a saved project to run
    python manage.py create   <project-id>
    python manage.py list
    python manage.py show     <project-id>
    python manage.py set-stt  <project-id> --provider <sarvam|deepgram> [--model MODEL]
    python manage.py set-tts  <project-id> --provider <sarvam|deepgram> [--model MODEL]
    python manage.py set-llm  <project-id> --model MODEL
    python manage.py delete   <project-id>
    python manage.py run      <project-id>
"""

import argparse
import json
import os
import sys
import subprocess
from pathlib import Path

# Enable ANSI escape sequences on Windows
os.system('')

class Colors:
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    BOLD = '\033[1m'
    RESET = '\033[0m'
    
CONFIG_FILE = Path(__file__).parent / "projects_config.json"

# ── Provider / model catalogue ────────────────────────────────────────────────
# Each entry:  (display_label, provider_key, model, language, speaker_or_None)
STT_OPTIONS = [
    ("Sarvam  - saaras:v3  (Hindi, recommended)",  "sarvam",   "saaras:v3", "hi-IN",  None),
    ("Deepgram - nova-2    (Hindi)",                 "deepgram", "nova-2",    "hi-IN",  None),
    ("Deepgram - nova-3    (Hindi, latest)",         "deepgram", "nova-3",    "hi-IN",  None),
    ("Deepgram - enhanced  (Hindi)",                 "deepgram", "enhanced",  "hi-IN",  None),
]

TTS_OPTIONS = [
    ("Sarvam  - bulbul:v3  voice: shubh  (Hindi, recommended)", "sarvam",   "bulbul:v3",        "hi-IN", "shubh"),
    ("Sarvam  - bulbul:v3  voice: meera  (Hindi, female)",       "sarvam",   "bulbul:v3",        "hi-IN", "meera"),
    ("Deepgram - aura-asteria-en  (English)",                     "deepgram", "aura-asteria-en",  "en-US", None),
    ("Deepgram - aura-luna-en     (English, soft)",               "deepgram", "aura-luna-en",     "en-US", None),
]

LLM_OPTIONS = [
    ("openai/gpt-4o-mini          (fast, cheap)",        "openai/gpt-4o-mini"),
    ("openai/gpt-4o               (smart, balanced)",    "openai/gpt-4o"),
    ("google/gemini-2.5-flash     (fast, multilingual)", "google/gemini-2.5-flash"),
    ("google/gemini-2.5-pro       (most capable)",       "google/gemini-2.5-pro"),
    ("anthropic/claude-3-haiku    (fast, concise)",      "anthropic/claude-3-haiku"),
]

# Backwards-compat defaults used by set-stt / set-tts commands
STT_DEFAULTS = {
    "sarvam":   {"model": "saaras:v3", "language": "hi-IN"},
    "deepgram":  {"model": "nova-2",   "language": "hi-IN"},
}
TTS_DEFAULTS = {
    "sarvam":   {"model": "bulbul:v3",      "language": "hi-IN", "speaker": "shubh"},
    "deepgram":  {"model": "aura-asteria-en", "language": "en-US", "speaker": None},
}
ALLOWED_STT_PROVIDERS = list(STT_DEFAULTS.keys())
ALLOWED_TTS_PROVIDERS = list(TTS_DEFAULTS.keys())


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_config() -> dict:
    """Load projects_config.json, creating it if missing."""
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text("{}", encoding="utf-8")
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


def save_config(data: dict):
    """Write config back to disk with pretty formatting."""
    CONFIG_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  {Colors.GREEN}[✔] Saved config to {CONFIG_FILE.name}{Colors.RESET}")


def print_project(pid: str, proj: dict):
    """Pretty-print a single project's config."""
    stt = proj.get("stt", {})
    tts = proj.get("tts", {})
    llm = proj.get("llm", {})
    print(f"\n  {Colors.CYAN}{Colors.BOLD}Project: {pid}{Colors.RESET}")
    print(f"  {'-'*50}")
    print(f"  {Colors.BOLD}STT{Colors.RESET}  provider : {Colors.YELLOW}{stt.get('provider', '?')}{Colors.RESET}")
    print(f"       model    : {stt.get('model', '?')}")
    print(f"       language : {stt.get('language', '?')}")
    print(f"  {'-'*50}")
    print(f"  {Colors.BOLD}TTS{Colors.RESET}  provider : {Colors.YELLOW}{tts.get('provider', '?')}{Colors.RESET}")
    print(f"       model    : {tts.get('model', '?')}")
    print(f"       language : {tts.get('language', '?')}")
    print(f"       speaker  : {tts.get('speaker', 'N/A')}")
    print(f"  {'-'*50}")
    print(f"  {Colors.BOLD}LLM{Colors.RESET}  model    : {Colors.YELLOW}{llm.get('model', '?')}{Colors.RESET}")
    print()


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_create(args):
    """Create a new project with sensible defaults."""
    cfg = load_config()
    pid = args.project_id

    if pid in cfg:
        print(f"  [error] Project '{pid}' already exists. Use 'show' or 'set-stt'/'set-tts' to modify.")
        sys.exit(1)

    cfg[pid] = {
        "stt": {"provider": "sarvam", **STT_DEFAULTS["sarvam"]},
        "tts": {"provider": "sarvam", **TTS_DEFAULTS["sarvam"]},
        "llm": {"model": "openai/gpt-4o-mini"},
    }
    save_config(cfg)
    print(f"  [created] Project '{pid}' with default Sarvam STT/TTS")
    print_project(pid, cfg[pid])


def cmd_list(args):
    """List all configured projects."""
    cfg = load_config()
    if not cfg:
        print(f"  {Colors.YELLOW}No projects configured. Use 'create <project-id>' to add one.{Colors.RESET}")
        return

    print(f"\n  {Colors.CYAN}{Colors.BOLD}{'Project ID':<20} | {'STT':<25} | {'TTS':<25} | {'LLM'}{Colors.RESET}")
    print(f"  {'-'*100}")
    for pid, proj in cfg.items():
        stt_info = f"{proj['stt']['provider']}/{proj['stt']['model']}"
        tts_info = f"{proj['tts']['provider']}/{proj['tts']['model']}"
        llm_info = proj.get('llm', {}).get('model', '?')
        print(f"  {Colors.BOLD}{pid:<20}{Colors.RESET} | {Colors.YELLOW}{stt_info:<25}{Colors.RESET} | {Colors.GREEN}{tts_info:<25}{Colors.RESET} | {llm_info}")
    print(f"\n  Total: {len(cfg)} project(s)\n")


def cmd_show(args):
    """Show full config for a project."""
    cfg = load_config()
    pid = args.project_id

    if pid not in cfg:
        print(f"  {Colors.RED}[error] Project '{pid}' not found. Run 'list' to see available projects.{Colors.RESET}")
        sys.exit(1)

    print_project(pid, cfg[pid])


def cmd_set_stt(args):
    """Set or update STT config for a project."""
    cfg = load_config()
    pid = args.project_id

    if pid not in cfg:
        print(f"  [error] Project '{pid}' not found. Create it first with 'create {pid}'.")
        sys.exit(1)

    provider = args.provider.lower()
    if provider not in ALLOWED_STT_PROVIDERS:
        print(f"  [error] Unknown STT provider '{provider}'. Allowed: {ALLOWED_STT_PROVIDERS}")
        sys.exit(1)

    defaults = STT_DEFAULTS[provider]
    cfg[pid]["stt"] = {
        "provider": provider,
        "model":    args.model or defaults["model"],
        "language": args.language or defaults["language"],
    }
    save_config(cfg)
    print(f"  [updated] STT for '{pid}'")
    print_project(pid, cfg[pid])


def cmd_set_tts(args):
    """Set or update TTS config for a project."""
    cfg = load_config()
    pid = args.project_id

    if pid not in cfg:
        print(f"  [error] Project '{pid}' not found. Create it first with 'create {pid}'.")
        sys.exit(1)

    provider = args.provider.lower()
    if provider not in ALLOWED_TTS_PROVIDERS:
        print(f"  [error] Unknown TTS provider '{provider}'. Allowed: {ALLOWED_TTS_PROVIDERS}")
        sys.exit(1)

    defaults = TTS_DEFAULTS[provider]
    cfg[pid]["tts"] = {
        "provider": provider,
        "model":    args.model or defaults["model"],
        "language": args.language or defaults["language"],
        "speaker":  args.speaker or defaults.get("speaker"),
    }
    save_config(cfg)
    print(f"  [updated] TTS for '{pid}'")
    print_project(pid, cfg[pid])


def cmd_set_llm(args):
    """Set the LLM model for a project."""
    cfg = load_config()
    pid = args.project_id

    if pid not in cfg:
        print(f"  [error] Project '{pid}' not found. Create it first with 'create {pid}'.")
        sys.exit(1)

    cfg[pid]["llm"] = {"model": args.model}
    save_config(cfg)
    print(f"  [updated] LLM for '{pid}' -> {args.model}")
    print_project(pid, cfg[pid])


def cmd_delete(args):
    """Delete a project config."""
    cfg = load_config()
    pid = args.project_id

    if pid not in cfg:
        print(f"  {Colors.RED}[!] Project '{pid}' not found.{Colors.RESET}")
        sys.exit(1)

    confirm = input(f"  Delete project '{pid}'? This cannot be undone. [y/N]: ").strip().lower()
    if confirm != "y":
        print(f"  {Colors.YELLOW}Cancelled.{Colors.RESET}")
        return

    del cfg[pid]
    save_config(cfg)
    print(f"  {Colors.GREEN}[✔] Deleted project '{pid}'{Colors.RESET}")


def cmd_start(args):
    """Interactive wizard: select a project to run."""
    cfg = load_config()
    if not cfg:
        print(f"  {Colors.RED}[!] No projects configured. Use 'manage.py create' to add one.{Colors.RESET}")
        return

    # Clear screen for a clean UI
    os.system('cls' if os.name == 'nt' else 'clear')

    print(f"\n  {Colors.CYAN}{Colors.BOLD}======================================================{Colors.RESET}")
    print(f"   {Colors.BOLD}🎙️  Voice Agent - Project Selector{Colors.RESET}")
    print(f"  {Colors.CYAN}{Colors.BOLD}======================================================{Colors.RESET}\n")
    
    options = list(cfg.keys())
    print(f"  {Colors.BOLD}Available Projects:{Colors.RESET}")
    print(f"  {'-' * 80}")
    for i, pid in enumerate(options, 1):
        proj = cfg[pid]
        stt = proj.get('stt', {})
        tts = proj.get('tts', {})
        stt_str = f"{stt.get('provider')}/{stt.get('model')}"
        tts_str = f"{tts.get('provider')}/{tts.get('model')}"
        print(f"  {Colors.CYAN}{i}.{Colors.RESET} {Colors.BOLD}{pid:<15}{Colors.RESET} (STT: {Colors.YELLOW}{stt_str:<20}{Colors.RESET} | TTS: {Colors.GREEN}{tts_str}{Colors.RESET})")
        
    print(f"  {'-' * 80}")
    while True:
        try:
            raw = input(f"\n  {Colors.BOLD}Select project to run (1-{len(options)}): {Colors.RESET}").strip()
            idx = int(raw) - 1
            if 0 <= idx < len(options):
                selected_pid = options[idx]
                break
            print(f"  {Colors.RED}[!] Please enter a number between 1 and {len(options)}{Colors.RESET}")
        except (ValueError, EOFError, KeyboardInterrupt):
            print(f"\n  {Colors.YELLOW}Cancelled.{Colors.RESET}")
            return

    print(f"\n  {Colors.GREEN}{Colors.BOLD}🚀 Starting agent with project '{selected_pid}'...{Colors.RESET}\n")

    # Pass selected project as env var
    env = os.environ.copy()
    env["PROJECT_ID"] = selected_pid

    agent_script = Path(__file__).parent / "livekit_basic_agent.py"
    try:
        subprocess.run(
            [sys.executable, str(agent_script), "console"],
            env=env,
            cwd=str(Path(__file__).parent),
        )
    except KeyboardInterrupt:
        pass  # Exit cleanly without printing a stack trace


def cmd_run(args):
    """Run the voice agent with a specific project's configuration."""
    cfg = load_config()
    pid = args.project_id

    if pid not in cfg:
        print(f"  [error] Project '{pid}' not found. Create it first.")
        sys.exit(1)

    print_project(pid, cfg[pid])
    print(f"  Starting agent with project '{pid}'...\n")

    # Set PROJECT_ID env var and launch the agent
    env = os.environ.copy()
    env["PROJECT_ID"] = pid

    agent_script = Path(__file__).parent / "livekit_basic_agent.py"
    try:
        subprocess.run(
            [sys.executable, str(agent_script), "console"],
            env=env,
            cwd=str(Path(__file__).parent),
        )
    except KeyboardInterrupt:
        pass  # Exit cleanly without printing a stack trace


# ── Argument Parser ───────────────────────────────────────────────────────────

def build_parser():
    parser = argparse.ArgumentParser(
        prog="manage.py",
        description="Manage voice-agent project configurations (STT/TTS providers & models).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python manage.py create acme-corp
  python manage.py set-stt acme-corp --provider deepgram --model nova-2 --language hi-IN
  python manage.py set-tts acme-corp --provider sarvam --model bulbul:v3 --speaker shubh
  python manage.py set-llm acme-corp --model google/gemini-2.5-flash
  python manage.py show acme-corp
  python manage.py list
  python manage.py run acme-corp
        """,
    )
    sub = parser.add_subparsers(dest="command", required=True, help="Available commands")

    # start (interactive)
    sub.add_parser("start", help="Interactive wizard: select a saved project and run it")

    # create
    p = sub.add_parser("create", help="Create a new project with default Sarvam config")
    p.add_argument("project_id", help="Unique project identifier (e.g. acme-corp)")

    # list
    sub.add_parser("list", help="List all configured projects")

    # show
    p = sub.add_parser("show", help="Show full config for a project")
    p.add_argument("project_id", help="Project ID to show")

    # set-stt
    p = sub.add_parser("set-stt", help="Set STT provider & model for a project")
    p.add_argument("project_id", help="Project ID")
    p.add_argument("--provider", required=True, choices=ALLOWED_STT_PROVIDERS, help="STT provider")
    p.add_argument("--model", default=None, help="STT model name (defaults to provider's default)")
    p.add_argument("--language", default=None, help="Language code (default: hi-IN)")

    # set-tts
    p = sub.add_parser("set-tts", help="Set TTS provider & model for a project")
    p.add_argument("project_id", help="Project ID")
    p.add_argument("--provider", required=True, choices=ALLOWED_TTS_PROVIDERS, help="TTS provider")
    p.add_argument("--model", default=None, help="TTS model name (defaults to provider's default)")
    p.add_argument("--language", default=None, help="Language code (default: hi-IN)")
    p.add_argument("--speaker", default=None, help="Speaker voice name (Sarvam only, e.g. shubh)")

    # set-llm
    p = sub.add_parser("set-llm", help="Set the LLM model for a project")
    p.add_argument("project_id", help="Project ID")
    p.add_argument("--model", required=True, help="LLM model identifier (e.g. openai/gpt-4o-mini)")

    # delete
    p = sub.add_parser("delete", help="Delete a project configuration")
    p.add_argument("project_id", help="Project ID to delete")

    # run
    p = sub.add_parser("run", help="Run the voice agent with a project's config")
    p.add_argument("project_id", help="Project ID to run")

    return parser


if __name__ == "__main__":
    parser = build_parser()
    args = parser.parse_args()

    commands = {
        "start":   cmd_start,
        "create":  cmd_create,
        "list":    cmd_list,
        "show":    cmd_show,
        "set-stt": cmd_set_stt,
        "set-tts": cmd_set_tts,
        "set-llm": cmd_set_llm,
        "delete":  cmd_delete,
        "run":     cmd_run,
    }

    commands[args.command](args)
