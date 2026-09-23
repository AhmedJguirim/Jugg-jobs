#!/usr/bin/env python
"""Shell-command audit log.

Deliberately standalone: this tool is NOT part of the application. It has no
dependency on `app/`, imports only the standard library, and can be copied to
another project as-is. Its job is to make automated/agent work auditable by
recording every shell command that was run, what it was for, and how dangerous
it was.

Two modes:

    # run the command AND record it (exit code and duration are captured)
    python tools/cmdlog.py run --risk low --note "install deps" -- pip install x

    # record a command that was executed elsewhere (e.g. by an agent's own tool)
    python tools/cmdlog.py log --risk none --note "listed files" -- ls -la

    # read the audit trail back
    python tools/cmdlog.py report
    python tools/cmdlog.py report --min-risk high
    python tools/cmdlog.py verify        # re-check stored risk against heuristics

Risk levels
-----------
    none     Read-only. Cannot change state. (ls, cat, grep, SELECT, git status)
    low      Writes only to scratch/derived state; trivially reversible.
             (mkdir, writing a new file, running tests, git add)
    medium   Changes tracked project state or installs software; reversible with
             effort. (pip install, git commit, editing tracked files, ALTER TABLE)
    high     Changes persistent data or history; recovery needs a backup or a
             force-push. (DELETE/UPDATE rows, alembic downgrade, git reset --hard,
             rm -rf of real files, killing processes)
    severe   Destroys a datastore or is effectively irreversible.
             (DROP DATABASE/TABLE, TRUNCATE, git push --force, format, rm -rf /)

The log is append-only. Two files are written side by side:
    logs/command_audit.log    human-readable, one block per command
    logs/command_audit.jsonl  machine-readable, one JSON object per command
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Optional

RISK_LEVELS = ["none", "low", "medium", "high", "severe"]
RISK_RANK = {name: index for index, name in enumerate(RISK_LEVELS)}

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(PROJECT_ROOT, "logs")
TEXT_LOG = os.path.join(LOG_DIR, "command_audit.log")
JSON_LOG = os.path.join(LOG_DIR, "command_audit.jsonl")

# Ordered most-dangerous-first: the first pattern that matches decides the
# heuristic risk, so `drop database` wins over the generic `psql` rule.
_RULES: list[tuple[str, str, str]] = [
    # severe
    (r"\bdrop\s+(database|schema|table)\b", "severe", "drops a database object"),
    (r"\btruncate\b", "severe", "truncates a table"),
    (r"\bdropdb\b", "severe", "drops a database"),
    (r"\bgit\s+push\b.*(--force|-f)\b", "severe", "force-push rewrites remote history"),
    (r"\brm\s+(-[a-z]*r[a-z]*f|-[a-z]*f[a-z]*r)\b.*\s/(\s|$)", "severe", "recursive delete from root"),
    (r"\bmkfs|format\s+[a-z]:", "severe", "formats a filesystem"),
    # high
    (r"\bdelete\s+from\b", "high", "deletes rows"),
    (r"^\s*update\s+\w+\s+set\b", "high", "updates rows in place"),
    (r"\balembic\s+downgrade\b", "high", "reverts a migration"),
    (r"\bgit\s+reset\s+--hard\b", "high", "discards working-tree changes"),
    (r"\bgit\s+clean\b.*-[a-z]*f", "high", "deletes untracked files"),
    (r"\bgit\s+checkout\s+--\s", "high", "discards file changes"),
    (r"\brm\s+-[a-z]*r", "high", "recursive delete"),
    (r"\b(taskkill|kill)\b", "high", "terminates a process"),
    (r"\bdocker\s+(rm|rmi|volume\s+rm)\b", "high", "removes a container/image/volume"),
    # medium
    (r"\bcreate\s+(database|schema|table|index)\b", "medium", "creates a database object"),
    (r"\bcreatedb\b", "medium", "creates a database"),
    (r"\balter\s+table\b", "medium", "alters a table"),
    (r"\b(insert\s+into|copy\s+\w+\s+from)\b", "medium", "writes rows"),
    (r"\balembic\s+(upgrade|revision)\b", "medium", "applies/creates a migration"),
    (r"\bpip\s+(install|uninstall)\b", "medium", "changes installed packages"),
    (r"\bnpm\s+(install|uninstall|ci)\b", "medium", "changes installed packages"),
    (r"\bplaywright\s+install\b", "medium", "downloads browser binaries"),
    (r"\bgit\s+(commit|merge|rebase|revert|stash)\b", "medium", "changes git history/state"),
    (r"\bmv\b|\bmove\b", "medium", "moves or renames files"),
    (r"\b(rm|del)\b", "medium", "deletes a file"),
    (r"\bchmod\b|\bicacls\b", "medium", "changes permissions"),
    # low
    (r">>?\s*\S+", "low", "redirects output to a file"),
    (r"\bmkdir\b|\btouch\b|\bcp\b|\bcopy\b", "low", "creates files/directories"),
    (r"\bpytest\b|\bnpm\s+test\b|\bplaywright\s+test\b", "low", "runs tests"),
    (r"\bgit\s+(add|fetch|switch|checkout)\b", "low", "changes index/branch state"),
    (r"\bpython\b|\bnode\b|\buvicorn\b", "low", "executes code"),
    # none
    (r"\bselect\b", "none", "read-only query"),
    (r"^\s*(ls|dir|cat|head|tail|grep|rg|find|wc|echo|pwd|which|type|env)\b", "none", "read-only"),
    (r"\bgit\s+(status|log|diff|show|branch)\b", "none", "read-only git"),
    (r"\bcurl\s+(-s\s+)?(-o\s+/dev/null|-I)\b", "none", "read-only request"),
    (r"\b(netstat|ps|tasklist)\b", "none", "read-only inspection"),
]


def classify(command: str) -> tuple[str, str]:
    """Best-effort risk guess for a command. Returns (risk, reason)."""
    text = command.lower()
    for pattern, risk, reason in _RULES:
        if re.search(pattern, text):
            return risk, reason
    return "low", "unrecognised command; defaulting to low"


def _ensure_log_dir() -> None:
    os.makedirs(LOG_DIR, exist_ok=True)


def _write(entry: dict) -> None:
    _ensure_log_dir()

    flag = ""
    if entry.get("risk_override"):
        flag = f"  [OVERRIDE: heuristic said {entry['heuristic_risk']}]"

    lines = [
        f"[{entry['timestamp']}] RISK={entry['risk'].upper():<6} {entry['mode'].upper()}{flag}",
        f"  cmd    : {entry['command']}",
        f"  why    : {entry['note'] or '-'}",
        f"  reason : {entry['risk_reason']}",
        f"  cwd    : {entry['cwd']}",
    ]
    if entry.get("exit_code") is not None:
        lines.append(f"  result : exit={entry['exit_code']} in {entry['duration_s']}s")
    lines.append("")

    with open(TEXT_LOG, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    with open(JSON_LOG, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _record(
    command: str,
    mode: str,
    note: str,
    risk: Optional[str],
    exit_code: Optional[int] = None,
    duration: Optional[float] = None,
) -> dict:
    heuristic_risk, reason = classify(command)
    final_risk = risk or heuristic_risk
    entry = {
        "timestamp": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "mode": mode,
        "command": command,
        "note": note,
        "risk": final_risk,
        "heuristic_risk": heuristic_risk,
        "risk_override": bool(risk) and risk != heuristic_risk,
        "risk_reason": reason,
        "cwd": os.getcwd(),
        "user": os.environ.get("USERNAME") or os.environ.get("USER"),
        "host": socket.gethostname(),
        "exit_code": exit_code,
        "duration_s": round(duration, 3) if duration is not None else None,
    }
    _write(entry)
    return entry


def _understate_warning(entry: dict) -> None:
    """Shout if a command was logged as safer than the heuristic believes."""
    if RISK_RANK[entry["risk"]] < RISK_RANK[entry["heuristic_risk"]]:
        print(
            f"cmdlog: WARNING - logged as '{entry['risk']}' but heuristics say "
            f"'{entry['heuristic_risk']}' ({entry['risk_reason']})",
            file=sys.stderr,
        )


def cmd_run(args: argparse.Namespace) -> int:
    command = " ".join(args.command)
    start = time.time()
    completed = subprocess.run(command, shell=True)
    duration = time.time() - start
    entry = _record(command, "run", args.note, args.risk, completed.returncode, duration)
    _understate_warning(entry)
    return completed.returncode


def cmd_log(args: argparse.Namespace) -> int:
    entry = _record(
        " ".join(args.command),
        "log",
        args.note,
        args.risk,
        exit_code=args.exit_code,
        duration=args.duration,
    )
    _understate_warning(entry)
    print(f"cmdlog: recorded risk={entry['risk']}")
    return 0


def cmd_classify(args: argparse.Namespace) -> int:
    risk, reason = classify(" ".join(args.command))
    print(f"{risk}\t{reason}")
    return 0


def _load_entries() -> list[dict]:
    if not os.path.exists(JSON_LOG):
        return []
    entries = []
    with open(JSON_LOG, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def cmd_report(args: argparse.Namespace) -> int:
    entries = _load_entries()
    if not entries:
        print("no commands recorded yet")
        return 0

    floor = RISK_RANK[args.min_risk]
    shown = [e for e in entries if RISK_RANK[e["risk"]] >= floor]

    counts = {level: 0 for level in RISK_LEVELS}
    for entry in entries:
        counts[entry["risk"]] += 1
    failures = [e for e in entries if e.get("exit_code") not in (None, 0)]

    print(f"{len(entries)} command(s) recorded")
    print("  " + "  ".join(f"{level}={counts[level]}" for level in RISK_LEVELS))
    print(f"  non-zero exits: {len(failures)}")
    print()
    for entry in shown[-args.limit :]:
        stamp = entry["timestamp"][:19].replace("T", " ")
        status = "" if entry.get("exit_code") in (None, 0) else f" !exit={entry['exit_code']}"
        print(f"{stamp}  {entry['risk'].upper():<6}{status}  {entry['command']}")
        if entry["note"]:
            print(f"{'':21}  -> {entry['note']}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Re-classify every logged command and report entries logged too leniently."""
    problems = []
    for entry in _load_entries():
        heuristic, reason = classify(entry["command"])
        if RISK_RANK[entry["risk"]] < RISK_RANK[heuristic]:
            problems.append((entry, heuristic, reason))

    if not problems:
        print("verify: OK - no command was logged below its heuristic risk")
        return 0
    print(f"verify: {len(problems)} entr(ies) logged below heuristic risk")
    for entry, heuristic, reason in problems:
        print(f"  {entry['timestamp']}  logged={entry['risk']} heuristic={heuristic} ({reason})")
        print(f"      {entry['command']}")
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cmdlog",
        description="Append-only audit log of shell commands and their risk.",
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    def add_risk(p: argparse.ArgumentParser) -> None:
        p.add_argument("--risk", choices=RISK_LEVELS, help="explicit risk; omit to auto-classify")
        p.add_argument("--note", default="", help="why this command was run")
        p.add_argument("command", nargs=argparse.REMAINDER, help="the command, after --")

    run = sub.add_parser("run", help="execute a command and log it")
    add_risk(run)
    run.set_defaults(func=cmd_run)

    log = sub.add_parser("log", help="log a command that was executed elsewhere")
    add_risk(log)
    log.add_argument("--exit-code", type=int, default=None)
    log.add_argument("--duration", type=float, default=None)
    log.set_defaults(func=cmd_log)

    classify_p = sub.add_parser("classify", help="show the heuristic risk of a command")
    classify_p.add_argument("command", nargs=argparse.REMAINDER)
    classify_p.set_defaults(func=cmd_classify)

    report = sub.add_parser("report", help="summarise the audit trail")
    report.add_argument("--min-risk", choices=RISK_LEVELS, default="none")
    report.add_argument("--limit", type=int, default=50)
    report.set_defaults(func=cmd_report)

    verify = sub.add_parser("verify", help="flag entries logged below their heuristic risk")
    verify.set_defaults(func=cmd_verify)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    # argparse.REMAINDER keeps a leading "--"; drop it.
    if getattr(args, "command", None) and args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if getattr(args, "command", None) is not None and not args.command:
        print("cmdlog: no command given (use -- before the command)", file=sys.stderr)
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
