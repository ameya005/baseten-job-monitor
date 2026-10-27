#!/usr/bin/env python3
# <swiftbar.hideAbout>true</swiftbar.hideAbout>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
# <swiftbar.hideDisablePlugin>true</swiftbar.hideDisablePlugin>

"""SwiftBar plugin: Baseten training job monitor. Refreshes every 1 minute.

AWS dev boxes live in the companion streaming plugin, devboxes.py.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime

# Ensure common tool directories are on PATH so truss is found
_extra_paths = [
    os.path.expanduser("~/.local/bin"),
    os.path.expanduser("~/.local/share/mise/shims"),
    "/opt/homebrew/bin",
    "/usr/local/bin",
]
os.environ["PATH"] = (
    ":".join(p for p in _extra_paths if os.path.isdir(p))
    + ":"
    + os.environ.get("PATH", "")
)

# truss uses Rich for tables; without a wide terminal it truncates every cell
# (Job ID, Status, Instance Type, etc.) to ~7 chars and switches the active-jobs
# section to a compact table that loses the "Training Job: <id>" headers our
# parser keys off. Force a wide width so the per-job detail blocks render.
os.environ["COLUMNS"] = "400"


def strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m|\x1b\].*?\x07", "", text)


def strip_box_chars(text: str) -> str:
    # Remove Unicode box-drawing characters
    return re.sub(r"[─│┌┐└┘├┤┬┴┼╭╮╰╯╶╴╷╵═║╔╗╚╝╠╣╦╩╬▪]", "", text)


def _split_box_row(line: str) -> list[str]:
    """Split a Rich table row on │ separators and strip each cell."""
    cells = [c.strip() for c in line.split("│")]
    # Drop empty leading/trailing cells from the outer borders.
    if cells and not cells[0]:
        cells = cells[1:]
    if cells and not cells[-1]:
        cells = cells[:-1]
    return cells


def _parse_active_jobs_blocks(raw: str) -> list[dict]:
    """Parse the legacy per-job 'Training Job: <id>' detail blocks (truss <=0.14)."""
    text = strip_ansi(raw)
    jobs = []
    blocks = re.split(r"Training Job:\s+(\S+)", text)
    for i in range(1, len(blocks), 2):
        job_id = blocks[i].strip()
        content = blocks[i + 1] if i + 1 < len(blocks) else ""
        content = strip_box_chars(content)
        job = {"job_id": job_id}
        for line in content.split("\n"):
            line = line.strip()
            if not line:
                continue
            parts = re.split(r"\s{2,}", line, maxsplit=1)
            if len(parts) == 2:
                key, val = parts[0].strip(), parts[1].strip()
                if key and val and key != val:
                    job[key] = val
        if "Status" in job:
            jobs.append(job)
    return jobs


def _parse_active_jobs_table(raw: str, projects: list[dict]) -> list[dict]:
    """Parse the single 'Active Training Jobs' Rich table (truss >=0.17).

    Columns: Job ID | Job Name | Project | Status | Instance Type | Created By | Created | Job Page
    """
    text = strip_ansi(raw)
    idx = text.find("Active Training Jobs")
    if idx < 0:
        return []
    section = text[idx:]

    name_to_pid = {p["name"]: p["project_id"] for p in projects}

    jobs = []
    headers = None
    for line in section.split("\n"):
        if "│" not in line:
            continue
        cells = _split_box_row(line)
        if not cells:
            continue
        if headers is None:
            # Header rows can wrap (e.g. " " then "Job ID", "Job Name"...). Wait
            # for the row that actually contains "Job ID".
            if any("Job ID" in c for c in cells):
                headers = cells
            continue
        # Data rows start with a 7-char alphanumeric job id.
        if not re.fullmatch(r"[a-z0-9]{7}", cells[0]):
            continue
        row = dict(zip(headers, cells))
        job = {
            "job_id": row.get("Job ID", cells[0]),
            "Job Name": row.get("Job Name", ""),
            "Project Name": row.get("Project", ""),
            "Status": row.get("Status", ""),
            "Instance Type": row.get("Instance Type", ""),
            "Created By": row.get("Created By", ""),
            "Created": row.get("Created", ""),
        }
        job["Project ID"] = name_to_pid.get(job["Project Name"], "")
        if job["Status"]:
            jobs.append(job)
    return jobs


def parse_active_jobs(raw: str, projects: list = None) -> list[dict]:
    """Parse the 'Active Training Jobs' section, supporting both truss formats."""
    jobs = _parse_active_jobs_blocks(raw)
    if jobs:
        # Older format omits Project ID inside the per-job block in some versions;
        # backfill from the projects table when possible.
        if projects:
            name_to_pid = {p["name"]: p["project_id"] for p in projects}
            for j in jobs:
                if not j.get("Project ID") and j.get("Project Name"):
                    j["Project ID"] = name_to_pid.get(j["Project Name"], "")
        return jobs
    return _parse_active_jobs_table(raw, projects or [])


def parse_projects(raw: str) -> list[dict]:
    """Parse the projects table to get project IDs and names."""
    text = strip_ansi(raw)
    # Do not let rows from the following jobs table masquerade as projects.
    section = text.split("Active Training Jobs", 1)[0]

    projects = []
    headers = None
    for line in section.split("\n"):
        if "│" not in line:
            continue
        cells = _split_box_row(line)
        if not cells:
            continue
        if headers is None:
            if any("Project ID" in c for c in cells):
                headers = cells
            continue
        if not re.fullmatch(r"[a-z0-9]{7}", cells[0]):
            continue
        row = dict(zip(headers, cells))
        projects.append({
            "project_id": row.get("Project ID", cells[0]),
            "name": row.get("Name", ""),
            "latest_job_id": row.get("Latest Job ID", ""),
        })

    if projects:
        return projects

    # Fall back to whitespace parsing for older Truss output.
    section = strip_box_chars(section)
    # Find lines that look like project rows (start with a project ID pattern)
    for line in section.split("\n"):
        line = line.strip()
        # Project IDs are 7-char alphanumeric
        match = re.match(r"^([a-z0-9]{7})\s+(.+)", line)
        if match:
            parts = re.split(r"\s{2,}", line)
            if len(parts) >= 5:
                projects.append({
                    "project_id": parts[0].strip(),
                    "name": parts[1].strip(),
                    "latest_job_id": parts[4].strip() if len(parts) > 4 else "",
                })

    return projects


GREEN, ORANGE, RED, GREY = "#34c759", "#ff9f0a", "#ff453a", "#8e8e93"


def sf_icon(symbol: str, color: str = "", scale: str = "medium") -> str:
    """SwiftBar params for a colored SF Symbol menu icon.

    SwiftBar colors `sfimage` icons through `sfconfig`, a base64 JSON blob
    (`sfcolor` only applies to symbols embedded inline in the text).
    """
    if not color:
        return f"sfimage={symbol}"
    cfg = {"renderingMode": "Hierarchical", "colors": [color], "scale": scale}
    b64 = base64.b64encode(json.dumps(cfg, separators=(",", ":")).encode()).decode()
    return f"sfimage={symbol} sfconfig={b64}"


def status_icon(status: str) -> str:
    """SwiftBar params for a Baseten job status icon (SF Symbol + color)."""
    s = status.upper()
    if "RUNNING" in s:
        return sf_icon("circle.fill", GREEN)
    if "CREATED" in s:
        return sf_icon("circle.fill", ORANGE)
    if "FAILED" in s:
        return sf_icon("circle.fill", RED)
    if "STOPPED" in s or "COMPLETED" in s:
        return sf_icon("circle.fill", GREY)
    return sf_icon("circle", GREY)


def parse_gpu_info(instance: str) -> str:
    """Extract GPU count and type from instance string like 'H200:8x192x2048 - 8 H200 GPUs'."""
    m = re.search(r"(\d+)\s+(H\d+|A\d+\w*)\s+GPU", instance)
    if m:
        return f"{m.group(1)}x{m.group(2)}"
    return ""


def total_gpus(jobs: list[dict]) -> int:
    """Sum GPU count across all active jobs."""
    total = 0
    for job in jobs:
        m = re.search(r"(\d+)\s+(?:H\d+|A\d+\w*)\s+GPU", job.get("Instance Type", ""))
        if m:
            total += int(m.group(1))
    return total


def short_status(status: str) -> str:
    return (
        status.replace("TRAINING_JOB_", "")
        .replace("_", " ")
        .title()
    )


def elapsed_time(created_at: str, now: datetime = None) -> str:
    """Return a compact duration since a Truss `Created` timestamp."""
    if not created_at:
        return ""

    value = created_at.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    elif value.endswith(" UTC"):
        value = value[:-4] + "+00:00"

    try:
        created = datetime.fromisoformat(value)
    except ValueError:
        return ""

    if now is None:
        now = datetime.now(created.tzinfo) if created.tzinfo else datetime.now()
    seconds = max(0, int((now - created).total_seconds()))
    minutes = seconds // 60
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)

    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return "<1m"


def parse_wandb_run_url(raw: str) -> str:
    """Extract the first concrete W&B run URL from training logs."""
    urls = re.findall(r"https://(?:www\.)?wandb\.ai/[^\s<>\"']+", strip_ansi(raw))
    for url in urls:
        url = url.rstrip(".,;:)]}")
        if "/runs/" in url:
            return url
    return ""


def notify(message: str, title: str = "Baseten Monitor") -> None:
    """Show a macOS notification for a menu action result."""
    message = message.replace('"', '\\"')
    title = title.replace('"', '\\"')
    subprocess.run(
        [
            "/usr/bin/osascript",
            "-e",
            f'display notification "{message}" with title "{title}"',
        ],
        capture_output=True,
        timeout=5,
    )


def open_wandb_run(job_id: str) -> None:
    """Resolve a job's W&B URL from its logs and open it in the browser."""
    if not re.fullmatch(r"[a-z0-9]{7}", job_id):
        notify(f"Invalid Baseten job ID: {job_id}")
        return

    try:
        result = subprocess.run(
            [
                "truss",
                "train",
                "logs",
                "--job-id",
                job_id,
                "--non-interactive",
            ],
            capture_output=True,
            text=True,
            timeout=45,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        notify(f"Could not read logs for {job_id}")
        return

    url = parse_wandb_run_url(result.stdout + result.stderr)
    if not url:
        notify(f"No W&B run URL found in logs for {job_id}")
        return

    subprocess.run(["/usr/bin/open", url], capture_output=True, timeout=5)


def main():
    try:
        result = subprocess.run(
            ["truss", "train", "view", "--non-interactive"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        raw = result.stdout + result.stderr
        if result.returncode != 0:
            print("BT: err")
            print("---")
            print(f"truss exited {result.returncode}")
            print(f"stderr: {result.stderr[:200]}")
            sys.exit(0)
    except FileNotFoundError:
        print("BT: no truss")
        print("---")
        print("truss not found in PATH")
        print(f"PATH: {os.environ.get('PATH', 'unset')}")
        sys.exit(0)
    except subprocess.TimeoutExpired:
        print("BT: timeout")
        print("---")
        print("truss timed out after 30s")
        sys.exit(0)

    projects = parse_projects(raw)
    jobs = parse_active_jobs(raw, projects)

    # Count active
    running = [j for j in jobs if "RUNNING" in j.get("Status", "").upper()]
    created = [j for j in jobs if "CREATED" in j.get("Status", "").upper()]

    # Menu bar title — show GPU count
    active = running or created
    gpus = total_gpus(active) if active else 0
    if running:
        gpu_str = f" ({gpus} GPUs)" if gpus else ""
        print(f"BT: {len(running)} running{gpu_str} | color=green")
    elif created:
        gpu_str = f" ({gpus} GPUs)" if gpus else ""
        print(f"BT: {len(created)} starting{gpu_str} | color=orange")
    elif jobs:
        print(f"BT: {len(jobs)} jobs")
    else:
        print("BT: idle")

    print("---")

    if not jobs:
        print("No active training jobs")
        print("---")

    for job in jobs:
        job_id = job.get("job_id", "?")
        job_name = job.get("Job Name", job_id)
        status = job.get("Status", "UNKNOWN")
        project_id = job.get("Project ID", "")
        project_name = job.get("Project Name", "")
        instance = job.get("Instance Type", "")
        created_by = job.get("Created By", "")
        created_at = job.get("Created", "")
        elapsed = elapsed_time(created_at)
        icon = status_icon(status)
        short = short_status(status)

        url = f"https://app.baseten.co/training/{project_id}/logs/{job_id}"

        gpu = parse_gpu_info(instance)
        gpu_label = f" [{gpu}]" if gpu else ""
        elapsed_label = f" · {elapsed}" if elapsed else ""
        print(f"{job_name} — {short}{elapsed_label}{gpu_label} | {icon} href={url}")
        print(f"--Job ID: {job_id} | font=monospace size=12")
        print(f"--Instance: {instance} | size=12")
        if created_by:
            print(f"--Launched by: {created_by} | size=12")
        if elapsed:
            duration_name = "Running for" if "RUNNING" in status.upper() else "Elapsed"
            print(f"--{duration_name}: {elapsed} | size=12")
        print(f"--Created: {created_at} | size=12")
        print(f"--Open Baseten Logs | href={url}")
        python = shlex.quote(sys.executable)
        script = shlex.quote(os.path.abspath(__file__))
        print(
            f"--Open W&B Run | bash={python} param1={script} "
            f"param2=--open-wandb param3={job_id} terminal=false refresh=false"
        )
        print(f"--Copy Job ID | bash=/bin/bash param1=-c param2='echo -n {job_id} | pbcopy' terminal=false")

    # Show all projects
    print("---")
    print("All Projects")
    for p in projects:
        pid = p["project_id"]
        name = p["name"]
        url = f"https://app.baseten.co/training/{pid}"
        print(f"--{name} ({pid}) | href={url}")

    print("---")
    print("Refresh | refresh=true")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--open-wandb":
        open_wandb_run(sys.argv[2])
    else:
        main()
