#!/Users/ameyajoshi/.local/share/mise/installs/python/3.12.12/bin/python3
# <swiftbar.hideAbout>true</swiftbar.hideAbout>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
# <swiftbar.hideDisablePlugin>true</swiftbar.hideDisablePlugin>

"""SwiftBar plugin: Baseten training job monitor. Refreshes every 1 minute."""

import os
import re
import subprocess
import sys

# Ensure mise shims are on PATH so truss is found
os.environ["PATH"] = (
    os.path.expanduser("~/.local/share/mise/shims")
    + ":"
    + os.environ.get("PATH", "")
)


def strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m|\x1b\].*?\x07", "", text)


def strip_box_chars(text: str) -> str:
    # Remove Unicode box-drawing characters
    return re.sub(r"[─│┌┐└┘├┤┬┴┼╭╮╰╯╶╴╷╵═║╔╗╚╝╠╣╦╩╬▪]", "", text)


def parse_active_jobs(raw: str) -> list[dict]:
    """Parse the 'Active Training Jobs' section from truss train view output."""
    text = strip_ansi(raw)
    jobs = []

    # Find each "Training Job: <id>" block
    blocks = re.split(r"Training Job:\s+(\S+)", text)
    # blocks[0] is before first match, then alternating: id, content, id, content...
    for i in range(1, len(blocks), 2):
        job_id = blocks[i].strip()
        content = blocks[i + 1] if i + 1 < len(blocks) else ""
        content = strip_box_chars(content)

        job = {"job_id": job_id}
        for line in content.split("\n"):
            line = line.strip()
            if not line:
                continue
            # Parse "Key  │  Value" or "Key    Value" patterns
            # After stripping box chars, it's "Key    Value"
            parts = re.split(r"\s{2,}", line, maxsplit=1)
            if len(parts) == 2:
                key = parts[0].strip()
                val = parts[1].strip()
                if key and val and key != val:
                    job[key] = val

        if "Status" in job:
            jobs.append(job)

    return jobs


def parse_projects(raw: str) -> list[dict]:
    """Parse the projects table to get project IDs and names."""
    text = strip_ansi(raw)
    text = strip_box_chars(text)

    projects = []
    # Find lines that look like project rows (start with a project ID pattern)
    for line in text.split("\n"):
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


def status_emoji(status: str) -> str:
    s = status.upper()
    if "RUNNING" in s:
        return "🟢"
    if "CREATED" in s:
        return "🟡"
    if "FAILED" in s or "DEPLOY_FAILED" in s:
        return "🔴"
    if "STOPPED" in s or "COMPLETED" in s:
        return "⚫"
    return "⚪"


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


def main():
    try:
        result = subprocess.run(
            [os.path.expanduser("~/.local/share/mise/installs/pipx-truss/0.14.1/bin/truss"), "train", "view", "--non-interactive"],
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

    jobs = parse_active_jobs(raw)
    projects = parse_projects(raw)

    # Build project lookup
    proj_lookup = {p["project_id"]: p for p in projects}

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
        created_at = job.get("Created", "")
        emoji = status_emoji(status)
        short = short_status(status)

        url = f"https://app.baseten.co/training/{project_id}/logs/{job_id}"

        gpu = parse_gpu_info(instance)
        gpu_label = f" [{gpu}]" if gpu else ""
        print(f"{emoji} {job_name} — {short}{gpu_label} | href={url}")
        print(f"--Job ID: {job_id} | font=monospace size=12")
        print(f"--Instance: {instance} | size=12")
        print(f"--Created: {created_at} | size=12")
        print(f"--Open in Browser | href={url}")
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
    main()
