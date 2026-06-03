#!/usr/bin/env python3
"""
Drop a log file in ./logs/ and inspect it — a cluster table by default, an
optional AI triage with --ai.

The ML engine is intentionally LLM-free — Drain3 + Isolation Forest produce the
clusters and anomaly flags, fully reproducibly. This script points the engine at
your file, pulls the clusters, and prints them as a table. With --ai it also asks
Claude to summarize the likely incidents and next steps. The math stays the
source of truth; the model only explains it.

Zero config:

  1. Make sure the engine is up:   docker compose up --build -d
  2. Drop your log file:           cp ~/Downloads/whatever.log ./logs/file.log
  3. Inspect it:                   python analyze.py            # table
                                   python analyze.py --ai       # table + AI triage

Any *.log / *.json / *.ndjson / *.csv in ./logs/ is picked up automatically
(file.log / app.log win if present). For --ai, the ANTHROPIC_API_KEY is read from
../../ml/.env (the same .env the engine uses) or from the environment.

    python analyze.py                       # table of all clusters
    python analyze.py --anomalies-only      # table of just the flagged clusters
    python analyze.py mylogs.log --ai       # specific file, plus AI triage
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import requests

MODEL = "claude-opus-4-8"

HERE = Path(__file__).resolve().parent
LOGS_DIR = HERE / "logs"                 # mounted into the container at /data (see docker-compose.yml)
# Where ./logs appears from the engine's point of view. Under docker-compose it's
# the /data mount; for a local (non-docker) engine, set RG_LOGS_MOUNT to the
# absolute path of ./logs so the engine reads the real file.
CONTAINER_LOGS = os.environ.get("RG_LOGS_MOUNT", "/data")
ENV_FILE = HERE.parent.parent / "ml" / ".env"

LOG_EXTS = (".log", ".json", ".ndjson", ".csv")
PREFERRED = ("file.log", "app.log")      # picked first when several files are present

# Visualization-only fields and verbose raw samples — dropped to keep the AI
# prompt focused on signal (template, service, counts, anomaly flag) and save tokens.
DROP_FIELDS = ("x", "y", "size", "color", "logs")

PROMPT = (
    "You are an experienced SRE triaging logs. Below are clustered log templates "
    "from our analyzer. Each cluster has a structural `template`, its `service`, "
    "`logCount`, an `avgScore`, and `isAnomaly` (true when Isolation Forest "
    "flagged it; anomalous clusters also carry `isolationDepth` — lower is more "
    "anomalous).\n\n"
    "Give me, concisely:\n"
    "1. The likely incident(s) and which services are involved.\n"
    "2. Root-cause hypotheses, ranked by confidence with one line of reasoning each.\n"
    "3. Concrete next steps — what to check or query first.\n\n"
    "Ground every claim in the clusters; do not invent log lines that aren't here.\n\n"
    "Clusters JSON:\n{clusters}"
)


def load_env_key() -> str:
    """ANTHROPIC_API_KEY from the environment, else from ../../ml/.env."""
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        return key
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            if name.strip() == "ANTHROPIC_API_KEY":
                return value.strip().strip('"').strip("'")
    return ""


def pick_log_file(explicit: str | None) -> Path:
    """Resolve which file in ./logs/ to analyze."""
    if explicit:
        p = Path(explicit)
        p = p if p.is_absolute() else LOGS_DIR / p
        if not p.exists():
            sys.exit(f"No such file: {p}")
        return p
    if not LOGS_DIR.exists():
        sys.exit(f"Logs dir not found: {LOGS_DIR}\nDrop your log file there as 'file.log' first.")
    candidates = [p for p in sorted(LOGS_DIR.iterdir()) if p.suffix.lower() in LOG_EXTS]
    if not candidates:
        sys.exit(f"No log files in {LOGS_DIR}.\nDrop one there, e.g.:  cp ~/Downloads/whatever.log {LOGS_DIR}/file.log")
    for name in PREFERRED:
        for c in candidates:
            if c.name == name:
                return c
    if len(candidates) > 1:
        names = ", ".join(c.name for c in candidates)
        print(f"==> Multiple log files found ({names}); using {candidates[0].name}. "
              f"Pass a name to choose.", file=sys.stderr)
    return candidates[0]


def container_path(local: Path) -> str:
    """Map a file under ./logs/ to the path the engine sees inside the container."""
    try:
        rel = local.resolve().relative_to(LOGS_DIR.resolve())
    except ValueError:
        sys.exit(f"File must live under {LOGS_DIR} (it's mounted into the engine). Got: {local}")
    return f"{CONTAINER_LOGS}/{rel.as_posix()}"


def point_engine_at(ml: str, path_in_container: str) -> None:
    resp = requests.post(
        f"{ml}/credentials",
        json={"source": "file", "credentials": {"path": path_in_container, "format": "auto"}},
        timeout=30,
    )
    resp.raise_for_status()


def fetch_clusters(ml: str) -> dict:
    resp = requests.get(f"{ml}/clusters", params={"source": "file"}, timeout=600)
    resp.raise_for_status()
    return resp.json()


def print_table(body: dict, anomalies_only: bool) -> None:
    clusters = body.get("clusters", [])
    anomalous = sum(c.get("isAnomaly") for c in clusters)
    rows = [c for c in clusters if c.get("isAnomaly")] if anomalies_only else clusters
    # anomalous first, then by logCount desc — the engine already sorts anomalous last, so re-sort here
    rows = sorted(rows, key=lambda c: (not c.get("isAnomaly"), -c.get("logCount", 0)))

    print(f"\n{body.get('log_count', '?')} logs → {body.get('cluster_count', len(clusters))} "
          f"clusters ({anomalous} anomalous)\n")
    if not rows:
        print("  (no clusters to show)")
        return

    svc_w = max(7, *(len(str(c.get("service", ""))) for c in rows))
    header = f"  {'ANOM':<4} {'SERVICE':<{svc_w}} {'LOGS':>6} {'DEPTH':>5}  TEMPLATE"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for c in rows:
        mark = " * " if c.get("isAnomaly") else "   "
        depth = c.get("isolationDepth")
        depth_s = f"{depth:>5}" if depth is not None else f"{'':>5}"
        tmpl = str(c.get("template", "")).replace("\n", " ")
        if len(tmpl) > 80:
            tmpl = tmpl[:77] + "..."
        print(f"  {mark:<4} {str(c.get('service','')):<{svc_w}} {c.get('logCount',0):>6} {depth_s}  {tmpl}")
    print()


def run_ai(body: dict, anomalies_only: bool) -> None:
    try:
        import anthropic
    except ImportError:
        sys.exit("--ai needs the anthropic SDK. Run:  pip install anthropic")

    api_key = load_env_key()
    if not api_key:
        sys.exit(f"--ai needs an ANTHROPIC_API_KEY. Add it to {ENV_FILE} "
                 f"(ANTHROPIC_API_KEY=sk-ant-...) or export it in your shell.")

    clusters = body.get("clusters", [])
    if anomalies_only:
        clusters = [c for c in clusters if c.get("isAnomaly")]
    if not clusters:
        sys.exit("No clusters to send to the model.")
    trimmed = [{k: v for k, v in c.items() if k not in DROP_FIELDS} for c in clusters]

    client = anthropic.Anthropic(api_key=api_key)
    print("==> Asking Claude for a triage...\n", file=sys.stderr)
    resp = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        messages=[{"role": "user", "content": PROMPT.format(clusters=json.dumps(trimmed, indent=2))}],
    )
    print(resp.content[0].text)


def main() -> None:
    ap = argparse.ArgumentParser(description="Inspect a log file's clusters; add --ai for a Claude triage.")
    ap.add_argument("file", nargs="?", help="log file under ./logs/ (or a path); auto-detected if omitted")
    ap.add_argument("--ai", action="store_true", help="also run an AI triage of the clusters")
    ap.add_argument("--anomalies-only", action="store_true", help="only the isAnomaly clusters (table and AI)")
    args = ap.parse_args()

    ml = os.environ.get("ML", "http://localhost:9020")
    try:
        requests.get(f"{ml}/health", timeout=5).raise_for_status()
    except requests.RequestException:
        sys.exit(f"ML engine not reachable at {ml}. Start it with:  docker compose up --build -d")

    log_file = pick_log_file(args.file)
    in_container = container_path(log_file)
    print(f"==> {log_file.name}  (engine reads it at {in_container})", file=sys.stderr)

    point_engine_at(ml, in_container)
    body = fetch_clusters(ml)

    print_table(body, args.anomalies_only)
    if args.ai:
        run_ai(body, args.anomalies_only)


if __name__ == "__main__":
    main()
