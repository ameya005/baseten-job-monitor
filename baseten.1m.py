#!/usr/bin/env python3
# <swiftbar.hideAbout>true</swiftbar.hideAbout>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
# <swiftbar.hideDisablePlugin>true</swiftbar.hideDisablePlugin>

"""SwiftBar plugin: Baseten training job + AWS dev box monitor. Refreshes every 1 minute.

AWS dev boxes are optional. They are configured per user in
~/.config/baseten-job-monitor/aws.env (see README). The plugin never stores or
reads AWS credentials itself; it calls the `aws` CLI, which uses whatever
sign-in you already have (SSO / `aws login`, named profiles, etc).
"""

from __future__ import annotations

import base64
import configparser
import json
import os
import re
import shlex
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
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
        return sf_icon("circle.dotted", ORANGE)
    if "FAILED" in s:
        return sf_icon("exclamationmark.triangle.fill", RED)
    if "COMPLETED" in s:
        return sf_icon("checkmark.circle.fill", GREY)
    if "STOPPED" in s:
        return sf_icon("stop.circle.fill", GREY)
    return sf_icon("circle", GREY)


# Backwards-compatible alias for anyone importing the old name.
status_emoji = status_icon


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


# --------------------------------------------------------------------------
# AWS dev boxes
# --------------------------------------------------------------------------
#
# Security model:
#   * The plugin never handles AWS credentials. Every AWS call goes through the
#     `aws` CLI, which uses the user's own sign-in (SSO / `aws login`, named
#     profiles, credential_process, ...). Nothing secret is written anywhere by
#     this script.
#   * The only per-user config is a small KEY=VALUE file that says which profile,
#     region, and instances to show. It is parsed as plain text (never sourced
#     or executed), must be owned by the user with mode 0600, and may not
#     contain credential-looking keys.
#   * Every subprocess call uses an argument list (no shell), and instance IDs /
#     names from config are validated against strict patterns.

AWS_CONFIG_DIR = os.path.expanduser("~/.config/baseten-job-monitor")
AWS_CONFIG_PATH = os.path.join(AWS_CONFIG_DIR, "aws.env")

# Keys the config file is allowed to set. Anything else is ignored.
AWS_ALLOWED_KEYS = {
    "AWS_PROFILE",
    "AWS_REGION",
    "BTMON_DEV_BOXES",
    "BTMON_SSH_USER",
    "BTMON_SSH_KEY",
}
# Keys that must never appear in the config file: credentials belong to the
# AWS CLI's own credential chain, not to this plugin.
AWS_FORBIDDEN_KEYS = {
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_SECURITY_TOKEN",
}

INSTANCE_ID_RE = re.compile(r"i-[0-9a-f]{8,17}")
# Name tags we accept from config: conservative charset, no whitespace.
INSTANCE_NAME_RE = re.compile(r"[A-Za-z0-9._\-]{1,128}")
SSH_USER_RE = re.compile(r"[A-Za-z0-9._\-]{1,32}")
AWS_TIMEOUT = 25


def _secure_file_problem(path: str) -> str:
    """Return a human-readable reason the config file is unsafe, or '' if fine."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return ""
    if st.st_uid != os.getuid():
        return "not owned by you"
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        return f"permissions are {oct(st.st_mode & 0o777)}, must be 0600"
    return ""


def load_aws_config() -> dict:
    """Load AWS dev box settings. Environment variables override the file.

    Returns a dict with keys: profile, region, boxes (list), ssh_user,
    configured (bool), error (str), warnings (list[str]).
    """
    cfg = {
        "profile": "",
        "region": "",
        "boxes": [],
        "ssh_user": "",
        "ssh_key": "",
        "configured": False,
        "error": "",
        "warnings": [],
    }
    values: dict[str, str] = {}

    if os.path.exists(AWS_CONFIG_PATH):
        problem = _secure_file_problem(AWS_CONFIG_PATH)
        if problem:
            cfg["error"] = f"Refusing to read {AWS_CONFIG_PATH}: {problem}"
            return cfg
        try:
            with open(AWS_CONFIG_PATH, encoding="utf-8") as fh:
                for lineno, line in enumerate(fh, 1):
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, val = line.partition("=")
                    key = key.strip().upper()
                    val = val.strip().strip("'\"")
                    if key in AWS_FORBIDDEN_KEYS:
                        cfg["error"] = (
                            f"{key} found in aws.env (line {lineno}). Credentials must not be "
                            "stored here; sign in with the AWS CLI instead."
                        )
                        return cfg
                    if key in AWS_ALLOWED_KEYS:
                        values[key] = val
                    else:
                        cfg["warnings"].append(f"Ignoring unknown key {key} in aws.env")
        except OSError as exc:
            cfg["error"] = f"Could not read aws.env: {exc}"
            return cfg

    # Environment overrides the file. BTMON_* vars are only honored from the
    # environment if set; AWS_PROFILE/AWS_REGION are standard AWS CLI vars.
    for key in AWS_ALLOWED_KEYS:
        if os.environ.get(key):
            values[key] = os.environ[key]

    boxes_raw = values.get("BTMON_DEV_BOXES", "")
    if not boxes_raw:
        return cfg  # Not configured; AWS section is simply hidden.

    for item in boxes_raw.split(","):
        item = item.strip()
        if not item:
            continue
        if INSTANCE_ID_RE.fullmatch(item) or INSTANCE_NAME_RE.fullmatch(item):
            cfg["boxes"].append(item)
        else:
            cfg["warnings"].append(f"Ignoring invalid dev box entry: {item[:40]!r}")

    if not cfg["boxes"]:
        cfg["error"] = "BTMON_DEV_BOXES has no valid instance IDs or names"
        return cfg

    profile = values.get("AWS_PROFILE", "").strip()
    if profile and not re.fullmatch(r"[A-Za-z0-9._\-]{1,64}", profile):
        cfg["error"] = "AWS_PROFILE contains unexpected characters"
        return cfg
    region = values.get("AWS_REGION", "").strip()
    if region and not re.fullmatch(r"[a-z]{2}(-[a-z]+)+-\d", region):
        cfg["error"] = f"AWS_REGION does not look like a region: {region!r}"
        return cfg
    ssh_user = values.get("BTMON_SSH_USER", "").strip()
    if ssh_user and not SSH_USER_RE.fullmatch(ssh_user):
        cfg["warnings"].append("Ignoring BTMON_SSH_USER: unexpected characters")
        ssh_user = ""

    # The SSH private key is only ever passed to `ssh -i`; the plugin never
    # reads it. We do insist that it is private to the user.
    ssh_key = os.path.expanduser(values.get("BTMON_SSH_KEY", "").strip())
    if ssh_key:
        if not os.path.isfile(ssh_key):
            cfg["warnings"].append(f"BTMON_SSH_KEY not found: {ssh_key}")
            ssh_key = ""
        else:
            problem = _secure_file_problem(ssh_key)
            if problem:
                cfg["warnings"].append(f"SSH key {problem}; run chmod 600 on it")
                ssh_key = ""

    cfg.update(
        profile=profile, region=region, ssh_user=ssh_user, ssh_key=ssh_key, configured=True
    )
    return cfg


def aws_base_cmd(cfg: dict) -> list[str]:
    cmd = ["aws", "--output", "json", "--no-cli-pager"]
    if cfg.get("profile"):
        cmd += ["--profile", cfg["profile"]]
    if cfg.get("region"):
        cmd += ["--region", cfg["region"]]
    return cmd


def aws_login_cmd(cfg: dict) -> list[str]:
    """Pick the right sign-in command for the configured profile.

    `aws login` (login_session) and `aws sso login` (sso_session / sso_start_url)
    are both SSO-style flows that leave no long-lived keys on disk.
    """
    profile = cfg.get("profile") or os.environ.get("AWS_PROFILE") or "default"
    section = "default" if profile == "default" else f"profile {profile}"
    parser = configparser.RawConfigParser()
    try:
        parser.read(os.path.expanduser("~/.aws/config"), encoding="utf-8")
    except (configparser.Error, OSError):
        pass
    keys = set(parser[section].keys()) if parser.has_section(section) else set()
    profile_args = ["--profile", profile] if cfg.get("profile") else []
    if "login_session" in keys:
        return ["aws", "login", *profile_args]
    if "sso_session" in keys or "sso_start_url" in keys:
        return ["aws", "sso", "login", *profile_args]
    # Unknown setup: `aws login` is the modern default and prints a clear error
    # if the profile isn't eligible.
    return ["aws", "login", *profile_args]


def _looks_like_auth_error(stderr: str) -> bool:
    s = stderr.lower()
    needles = (
        "token has expired",
        "expiredtoken",
        "unable to locate credentials",
        "sso session",
        "the sso session",
        "aws login",
        "aws sso login",
        "invalid grant",
        "refresh token",
        "could not refresh",
        "no credentials",
        "credentials were not found",
    )
    return any(n in s for n in needles)


def _run_aws(cmd: list[str]) -> tuple[int, str, str]:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=AWS_TIMEOUT)
    except FileNotFoundError:
        return 127, "", "aws CLI not found in PATH"
    except subprocess.TimeoutExpired:
        return 124, "", f"aws timed out after {AWS_TIMEOUT}s"
    return result.returncode, result.stdout, result.stderr


def _instance_from_json(inst: dict) -> dict:
    tags = {t.get("Key"): t.get("Value") for t in inst.get("Tags", []) or []}
    az = inst.get("Placement", {}).get("AvailabilityZone", "")
    region = az[:-1] if re.fullmatch(r"[a-z]{2}(-[a-z]+)+-\d[a-z]", az) else ""
    return {
        "id": inst.get("InstanceId", ""),
        "name": tags.get("Name", "") or inst.get("InstanceId", ""),
        "state": inst.get("State", {}).get("Name", "unknown"),
        "type": inst.get("InstanceType", ""),
        "public_ip": inst.get("PublicIpAddress", ""),
        "public_dns": inst.get("PublicDnsName", ""),
        "private_ip": inst.get("PrivateIpAddress", ""),
        "launch_time": inst.get("LaunchTime", ""),
        "az": az,
        "region": region,
        "status_checks": "",
    }


def fetch_dev_boxes(cfg: dict, with_metrics: bool = True) -> dict:
    """Query EC2 for the configured dev boxes.

    Returns {"boxes": [...], "error": str, "auth_error": bool}. When
    with_metrics is set, running boxes also get live CPU/RAM/GPU usage via SSH.
    """
    out = {"boxes": [], "error": "", "auth_error": False}
    ids = [b for b in cfg["boxes"] if INSTANCE_ID_RE.fullmatch(b)]
    names = [b for b in cfg["boxes"] if not INSTANCE_ID_RE.fullmatch(b)]

    # Filters (rather than --instance-ids) so a stale/typo'd ID doesn't fail
    # the whole call. Filters AND together, so IDs and names need two calls.
    calls = []
    if ids:
        calls.append(["--filters", f"Name=instance-id,Values={','.join(ids)}"])
    if names:
        calls.append(["--filters", f"Name=tag:Name,Values={','.join(names)}"])

    found: dict[str, dict] = {}
    for extra in calls:
        code, stdout, stderr = _run_aws(aws_base_cmd(cfg) + ["ec2", "describe-instances", *extra])
        if code != 0:
            out["error"] = stderr.strip().splitlines()[-1][:200] if stderr.strip() else f"aws exited {code}"
            out["auth_error"] = _looks_like_auth_error(stderr)
            return out
        try:
            data = json.loads(stdout or "{}")
        except json.JSONDecodeError:
            out["error"] = "Could not parse aws output"
            return out
        for res in data.get("Reservations", []):
            for inst in res.get("Instances", []):
                box = _instance_from_json(inst)
                if box["state"] == "terminated":
                    continue
                found[box["id"]] = box

    # Status checks for running boxes (one extra call, best effort).
    running_ids = [b["id"] for b in found.values() if b["state"] == "running"]
    if running_ids:
        code, stdout, _ = _run_aws(
            aws_base_cmd(cfg)
            + ["ec2", "describe-instance-status", "--instance-ids", *running_ids]
        )
        if code == 0:
            try:
                for st in json.loads(stdout or "{}").get("InstanceStatuses", []):
                    iid = st.get("InstanceId")
                    if iid in found:
                        sys_ok = st.get("SystemStatus", {}).get("Status", "")
                        inst_ok = st.get("InstanceStatus", {}).get("Status", "")
                        found[iid]["status_checks"] = f"system={sys_ok} instance={inst_ok}"
                        found[iid]["impaired"] = "impaired" in (sys_ok + inst_ok)
            except json.JSONDecodeError:
                pass

    # Keep config order; report entries that didn't resolve.
    ordered = []
    for entry in cfg["boxes"]:
        matches = [
            b for b in found.values()
            if b["id"] == entry or b["name"] == entry
        ]
        for m in matches:
            if m not in ordered:
                ordered.append(m)
        if not matches:
            ordered.append({"id": entry, "name": entry, "state": "not found", "missing": True})

    # Live usage over SSH for running boxes, all at once.
    if with_metrics:
        targets = [
            b for b in ordered
            if b.get("state") == "running" and (b.get("public_dns") or b.get("public_ip"))
        ]
        if targets:
            with ThreadPoolExecutor(max_workers=min(8, len(targets))) as pool:
                for box, res in zip(targets, pool.map(lambda b: fetch_box_metrics(cfg, b), targets)):
                    box.update(res)

    out["boxes"] = ordered
    return out


# Runs on the box over SSH (fed to `sh` via stdin, so no quoting issues).
# Prints one metric per line; nvidia-smi lines only appear on GPU boxes.
REMOTE_METRICS_SCRIPT = r"""
set -- $(head -1 /proc/stat); u1=$(( $2+$3+$4+$7+$8+$9 )); i1=$(( $5+$6 ))
sleep 0.6
set -- $(head -1 /proc/stat); u2=$(( $2+$3+$4+$7+$8+$9 )); i2=$(( $5+$6 ))
t=$(( (u2-u1)+(i2-i1) )); if [ "$t" -gt 0 ]; then cpu=$(( 100*(u2-u1)/t )); else cpu=0; fi
echo "CPU $cpu $(nproc 2>/dev/null || echo 0) $(cut -d' ' -f1-3 /proc/loadavg)"
free -m 2>/dev/null | awk '/^Mem:/{print "MEM",$3,$2} /^Swap:/{print "SWAP",$3,$2}'
df -Pm / 2>/dev/null | awk 'NR==2{print "DISK",$3,$2}'
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu \
    --format=csv,noheader,nounits 2>/dev/null | sed 's/^/GPU /'
fi
"""
METRICS_TIMEOUT = 15

# Instance families that normally carry NVIDIA GPUs (used only as a hint for labels).
GPU_FAMILY_RE = re.compile(r"^(p[2-5]|g[3-6]|gr6|g5g)[a-z]*\.", re.I)


def ssh_base_cmd(cfg: dict, host: str, batch: bool = True) -> list[str]:
    """Build the ssh command line used for both interactive and batch calls."""
    cmd = ["ssh"]
    if cfg.get("ssh_key"):
        cmd += ["-i", cfg["ssh_key"]]
    if batch:
        cmd += [
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=6",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "LogLevel=ERROR",
        ]
    target = f"{cfg['ssh_user']}@{host}" if cfg.get("ssh_user") else host
    cmd.append(target)
    return cmd


def parse_metrics(raw: str) -> dict:
    """Turn REMOTE_METRICS_SCRIPT output into a dict."""
    m: dict = {"gpus": []}
    for line in raw.splitlines():
        parts = line.split()
        if not parts:
            continue
        kind = parts[0]
        try:
            if kind == "CPU" and len(parts) >= 3:
                m["cpu_pct"] = int(parts[1])
                m["cores"] = int(parts[2])
                m["load"] = " ".join(parts[3:6])
            elif kind == "MEM" and len(parts) >= 3:
                m["mem_used_mb"], m["mem_total_mb"] = int(parts[1]), int(parts[2])
            elif kind == "SWAP" and len(parts) >= 3:
                m["swap_used_mb"], m["swap_total_mb"] = int(parts[1]), int(parts[2])
            elif kind == "DISK" and len(parts) >= 3:
                m["disk_used_mb"], m["disk_total_mb"] = int(parts[1]), int(parts[2])
            elif kind == "GPU":
                cells = [c.strip() for c in line[4:].split(",")]
                if len(cells) >= 5:
                    m["gpus"].append({
                        "index": cells[0],
                        "name": re.sub(r"^NVIDIA\s+", "", cells[1]),
                        "util_pct": int(cells[2]) if cells[2].isdigit() else None,
                        "vram_used_mb": int(cells[3]) if cells[3].isdigit() else None,
                        "vram_total_mb": int(cells[4]) if cells[4].isdigit() else None,
                        "temp_c": int(cells[5]) if len(cells) > 5 and cells[5].isdigit() else None,
                    })
        except ValueError:
            continue
    return m


def fetch_box_metrics(cfg: dict, box: dict) -> dict:
    """SSH into a running box and collect CPU/RAM/disk/GPU usage.

    Returns {"metrics": {...}} or {"metrics_error": str}.
    """
    host = box.get("public_dns") or box.get("public_ip")
    if not host:
        return {"metrics_error": "no public address for SSH"}
    cmd = ssh_base_cmd(cfg, host) + ["sh"]
    try:
        result = subprocess.run(
            cmd, input=REMOTE_METRICS_SCRIPT, capture_output=True, text=True,
            timeout=METRICS_TIMEOUT,
        )
    except FileNotFoundError:
        return {"metrics_error": "ssh not found"}
    except subprocess.TimeoutExpired:
        return {"metrics_error": f"ssh timed out after {METRICS_TIMEOUT}s"}
    if result.returncode != 0:
        err = result.stderr.strip().splitlines()
        return {"metrics_error": (err[-1] if err else f"ssh exited {result.returncode}")[:120]}
    metrics = parse_metrics(result.stdout)
    if "cpu_pct" not in metrics and "mem_total_mb" not in metrics:
        return {"metrics_error": "no metrics returned"}
    return {"metrics": metrics}


def usage_bar(pct: float | None, width: int = 10) -> str:
    if pct is None:
        return "░" * width
    filled = max(0, min(width, round(pct / 100 * width)))
    return "█" * filled + "░" * (width - filled)


def _gb(mb: int | None) -> str:
    return "?" if mb is None else f"{mb / 1024:.0f}" if mb >= 10240 else f"{mb / 1024:.1f}"


def _pct(used: int | None, total: int | None) -> float | None:
    if used is None or not total:
        return None
    return 100.0 * used / total


def _usage_color(pct: float | None) -> str:
    if pct is None:
        return "#8e8e93"
    if pct >= 90:
        return "#ff453a"
    if pct >= 70:
        return "#ff9f0a"
    return "#34c759"


def metrics_summary(metrics: dict) -> str:
    """Compact 'CPU 32% · RAM 41% · GPU 87%' for the box's main line."""
    bits = []
    if metrics.get("cpu_pct") is not None:
        bits.append(f"CPU {metrics['cpu_pct']}%")
    mem = _pct(metrics.get("mem_used_mb"), metrics.get("mem_total_mb"))
    if mem is not None:
        bits.append(f"RAM {mem:.0f}%")
    utils = [g["util_pct"] for g in metrics.get("gpus", []) if g.get("util_pct") is not None]
    if utils:
        bits.append(f"GPU {max(utils)}%")
    return " · ".join(bits)


def print_metrics_lines(metrics: dict) -> None:
    """Submenu lines (prefixed with --) that read like a tiny htop."""
    mono = "font=Menlo size=11"

    cpu = metrics.get("cpu_pct")
    cores = metrics.get("cores") or 0
    load = metrics.get("load", "")
    cores_label = f"{cores} cores" if cores else ""
    load_label = f"load {load}" if load else ""
    tail = " · ".join(x for x in (cores_label, load_label) if x)
    print(
        f"--CPU  {usage_bar(cpu)} {cpu if cpu is not None else '?':>3}%  {tail} "
        f"| {mono} {sf_icon('cpu', _usage_color(cpu))}"
    )

    mem = _pct(metrics.get("mem_used_mb"), metrics.get("mem_total_mb"))
    if mem is not None:
        print(
            f"--RAM  {usage_bar(mem)} {mem:>3.0f}%  "
            f"{_gb(metrics['mem_used_mb'])}/{_gb(metrics['mem_total_mb'])} GB "
            f"| {mono} {sf_icon('memorychip', _usage_color(mem))}"
        )
    swap = _pct(metrics.get("swap_used_mb"), metrics.get("swap_total_mb"))
    if swap is not None and metrics.get("swap_used_mb", 0) > 0:
        print(
            f"--Swap {usage_bar(swap)} {swap:>3.0f}%  "
            f"{_gb(metrics['swap_used_mb'])}/{_gb(metrics['swap_total_mb'])} GB "
            f"| {mono} {sf_icon('memorychip', _usage_color(swap))}"
        )

    for g in metrics.get("gpus", []):
        util = g.get("util_pct")
        vram = _pct(g.get("vram_used_mb"), g.get("vram_total_mb"))
        temp = f" · {g['temp_c']}°C" if g.get("temp_c") is not None else ""
        label = f"GPU{g['index']}" if len(metrics["gpus"]) > 1 else "GPU "
        print(
            f"--{label} {usage_bar(util)} {util if util is not None else '?':>3}%  "
            f"{g['name']}{temp} | {mono} {sf_icon('rectangle.3.group', _usage_color(util))}"
        )
        print(
            f"--VRAM {usage_bar(vram)} {vram if vram is not None else 0:>3.0f}%  "
            f"{_gb(g['vram_used_mb'])}/{_gb(g['vram_total_mb'])} GB "
            f"| {mono} {sf_icon('memorychip.fill', _usage_color(vram))}"
        )

    disk = _pct(metrics.get("disk_used_mb"), metrics.get("disk_total_mb"))
    if disk is not None:
        print(
            f"--Disk {usage_bar(disk)} {disk:>3.0f}%  "
            f"{_gb(metrics['disk_used_mb'])}/{_gb(metrics['disk_total_mb'])} GB on / "
            f"| {mono} {sf_icon('internaldrive', _usage_color(disk))}"
        )


def ec2_state_icon(box: dict) -> str:
    """SwiftBar params for an EC2 state icon (SF Symbol + color)."""
    if box.get("missing"):
        return sf_icon("questionmark.circle", GREY)
    if box.get("impaired"):
        return sf_icon("exclamationmark.triangle.fill", RED)
    state = box.get("state", "")
    return {
        "running": sf_icon("circle.fill", GREEN),
        "pending": sf_icon("circle.dotted", ORANGE),
        "stopping": sf_icon("circle.lefthalf.filled", ORANGE),
        "shutting-down": sf_icon("circle.lefthalf.filled", ORANGE),
        "stopped": sf_icon("stop.circle.fill", GREY),
        "terminated": sf_icon("xmark.circle.fill", RED),
    }.get(state, sf_icon("circle", GREY))


def ec2_console_url(box: dict, cfg: dict) -> str:
    region = box.get("region") or cfg.get("region") or "us-east-1"
    return (
        f"https://{region}.console.aws.amazon.com/ec2/home?region={region}"
        f"#InstanceDetails:instanceId={box['id']}"
    )


def aws_title_fragment(cfg: dict, fetched: dict) -> tuple[str, str]:
    """Return (text, color_hint) for the menu bar title, or ('', '')."""
    if not cfg["configured"]:
        if cfg["error"]:
            return "EC2: config err", "red"
        return "", ""
    if fetched["auth_error"]:
        return "EC2: sign in", "orange"
    if fetched["error"]:
        return "EC2: err", "red"
    boxes = fetched["boxes"]
    running = [b for b in boxes if b.get("state") == "running"]
    if any(b.get("impaired") for b in running):
        return f"EC2: {len(running)}/{len(boxes)} up ⚠", "red"
    if running:
        return f"EC2: {len(running)}/{len(boxes)} up", ""
    return f"EC2: 0/{len(boxes)} up", ""


def print_aws_section(cfg: dict, fetched: dict) -> None:
    python = shlex.quote(sys.executable)
    script = shlex.quote(os.path.abspath(__file__))

    print("---")
    scope = " · ".join(x for x in (cfg.get("profile"), cfg.get("region")) if x)
    header = "AWS Dev Boxes" + (f" ({scope})" if scope else "")

    if not cfg["configured"]:
        print(header)
        if cfg["error"]:
            print(f"--{cfg['error']} | {sf_icon('exclamationmark.triangle.fill', RED)} color=red size=12")
            if "permissions" in cfg["error"]:
                print(
                    f"--Fix permissions (chmod 600) | bash=/bin/chmod param1=600 "
                    f"param2={shlex.quote(AWS_CONFIG_PATH)} terminal=false refresh=true"
                )
        else:
            print("--Not configured | size=12")
        print(
            f"--Set up AWS dev boxes… | bash={python} param1={script} "
            f"param2=--aws-setup terminal=true refresh=true"
        )
        return

    print(header)
    for w in cfg["warnings"]:
        print(f"--{w} | {sf_icon('exclamationmark.triangle', ORANGE)} color=orange size=12")

    if fetched["auth_error"]:
        login = " ".join(aws_login_cmd(cfg))
        print(f"AWS sign-in required | {sf_icon('lock.fill', ORANGE)} color=orange")
        print(f"--{fetched['error']} | size=12")
        print(
            f"--Sign in ({login}) | bash={python} param1={script} "
            f"param2=--aws-login terminal=true refresh=true"
        )
        return
    if fetched["error"]:
        print(f"{fetched['error']} | {sf_icon('exclamationmark.triangle.fill', RED)} color=red")
        return

    for box in fetched["boxes"]:
        icon = ec2_state_icon(box)
        name = box.get("name", box["id"])
        state = box.get("state", "unknown")
        if box.get("missing"):
            print(f"{name} — not found (check aws.env) | {icon} color=gray")
            continue

        up = elapsed_time(box.get("launch_time", "")) if state == "running" else ""
        up_label = f" · up {up}" if up else ""
        type_label = f" [{box['type']}]" if box.get("type") else ""
        url = ec2_console_url(box, cfg)

        # Clicking a box connects to it: SSH when it has a public address,
        # SSM otherwise. Stopped boxes open the console instead.
        has_public = bool(box.get("public_ip") or box.get("public_dns"))
        if state == "running" and has_public:
            click = f"bash={python} param1={script} param2=--aws-ssh param3={box['id']} terminal=true"
        elif state == "running":
            click = f"bash={python} param1={script} param2=--aws-ssm param3={box['id']} terminal=true"
        else:
            click = f"href={url}"
        metrics = box.get("metrics")
        summary = f" · {metrics_summary(metrics)}" if metrics else ""
        print(f"{name} — {state}{up_label}{type_label}{summary} | {icon} {click}")

        if metrics:
            print_metrics_lines(metrics)
            print("-----")
        elif box.get("metrics_error") and state == "running":
            print(
                f"--Usage unavailable: {box['metrics_error']} "
                f"| {sf_icon('exclamationmark.triangle', GREY)} color=gray size=11"
            )
            print("-----")

        gpu_hint = " · GPU instance" if GPU_FAMILY_RE.match(box.get("type", "")) and not (metrics or {}).get("gpus") else ""
        print(f"--Instance ID: {box['id']} | font=monospace size=12")
        if box.get("type"):
            print(f"--Type: {box['type']} · {box.get('az', '')}{gpu_hint} | size=12")
        if box.get("public_ip"):
            print(f"--Public IP: {box['public_ip']} | font=monospace size=12")
        if box.get("private_ip"):
            print(f"--Private IP: {box['private_ip']} | font=monospace size=12")
        if box.get("status_checks"):
            print(f"--Status checks: {box['status_checks']} | size=12")
        if box.get("launch_time"):
            print(f"--Launched: {box['launch_time']} | size=12")
        print("-----")
        print(f"--Open in AWS Console | href={url}")
        if state == "running":
            print(
                f"--Connect via SSM (Terminal) | bash={python} param1={script} "
                f"param2=--aws-ssm param3={box['id']} terminal=true"
            )
            if box.get("public_ip") or box.get("public_dns"):
                print(
                    f"--SSH (Terminal) | sfimage=terminal bash={python} param1={script} "
                    f"param2=--aws-ssh param3={box['id']} terminal=true"
                )
                print(
                    f"--Open htop (Terminal) | sfimage=gauge.with.dots.needle.67percent bash={python} param1={script} "
                    f"param2=--aws-htop param3={box['id']} terminal=true"
                )
        if box.get("public_ip"):
            print(
                f"--Copy Public IP | bash=/bin/bash param1=-c "
                f"param2='echo -n {box['public_ip']} | pbcopy' terminal=false"
            )
        print(
            f"--Copy Instance ID | bash=/bin/bash param1=-c "
            f"param2='echo -n {box['id']} | pbcopy' terminal=false"
        )
        if state == "stopped":
            print(
                f"--Start instance | sfimage=play.fill bash={python} param1={script} "
                f"param2=--aws-start param3={box['id']} terminal=false refresh=true"
            )
        elif state == "running":
            print(
                f"--Stop instance… | sfimage=stop.fill bash={python} param1={script} "
                f"param2=--aws-stop param3={box['id']} terminal=false refresh=true"
            )

    print(
        f"--Edit AWS dev box settings… | bash={python} param1={script} "
        f"param2=--aws-setup terminal=true refresh=true"
    )


def _confirm_dialog(message: str, button: str) -> bool:
    """Ask the user to confirm a state-changing action via a native dialog."""
    message = message.replace('"', '\\"')
    script = (
        f'display dialog "{message}" with title "Baseten Monitor" '
        f'buttons {{"Cancel", "{button}"}} default button "Cancel" '
        f'cancel button "Cancel" with icon caution'
    )
    result = subprocess.run(
        ["/usr/bin/osascript", "-e", script], capture_output=True, text=True, timeout=120
    )
    return result.returncode == 0 and button in result.stdout


def _find_box(cfg: dict, instance_id: str) -> dict | None:
    if not INSTANCE_ID_RE.fullmatch(instance_id):
        notify(f"Invalid instance ID: {instance_id[:40]}")
        return None
    fetched = fetch_dev_boxes({**cfg, "boxes": [instance_id]}, with_metrics=False)
    if fetched["error"]:
        notify(fetched["error"], "AWS Dev Boxes")
        return None
    for b in fetched["boxes"]:
        if b.get("id") == instance_id and not b.get("missing"):
            return b
    notify(f"Instance {instance_id} not found", "AWS Dev Boxes")
    return None


def aws_action(action: str, instance_id: str) -> None:
    cfg = load_aws_config()
    if cfg["error"]:
        notify(cfg["error"], "AWS Dev Boxes")
        return

    if action == "--aws-login":
        cmd = aws_login_cmd(cfg)
        print(f"$ {' '.join(shlex.quote(c) for c in cmd)}")
        os.execvp(cmd[0], cmd)

    # Allow actions even when the instance isn't in the user's list, as long as
    # it is a well-formed ID and visible to their credentials.
    if not INSTANCE_ID_RE.fullmatch(instance_id):
        notify(f"Invalid instance ID: {instance_id[:40]}")
        return

    if action == "--aws-start":
        code, _, stderr = _run_aws(
            aws_base_cmd(cfg) + ["ec2", "start-instances", "--instance-ids", instance_id]
        )
        notify(
            f"Starting {instance_id}" if code == 0 else f"Start failed: {stderr.strip()[-160:]}",
            "AWS Dev Boxes",
        )
    elif action == "--aws-stop":
        box = _find_box(cfg, instance_id)
        if not box:
            return
        if not _confirm_dialog(
            f"Stop dev box \"{box['name']}\" ({instance_id})?\n\n"
            "Anything running on it will be interrupted.",
            "Stop",
        ):
            return
        code, _, stderr = _run_aws(
            aws_base_cmd(cfg) + ["ec2", "stop-instances", "--instance-ids", instance_id]
        )
        notify(
            f"Stopping {box['name']}" if code == 0 else f"Stop failed: {stderr.strip()[-160:]}",
            "AWS Dev Boxes",
        )
    elif action == "--aws-ssm":
        cmd = ["aws"]
        if cfg.get("profile"):
            cmd += ["--profile", cfg["profile"]]
        if cfg.get("region"):
            cmd += ["--region", cfg["region"]]
        cmd += ["ssm", "start-session", "--target", instance_id]
        print(f"$ {' '.join(shlex.quote(c) for c in cmd)}")
        os.execvp(cmd[0], cmd)
    elif action in ("--aws-ssh", "--aws-htop"):
        box = _find_box(cfg, instance_id)
        if not box:
            return
        host = box.get("public_dns") or box.get("public_ip")
        if not host:
            print(f"{box['name']} has no public address; use SSM instead.")
            return
        cmd = ssh_base_cmd(cfg, host, batch=False)
        if action == "--aws-htop":
            # -t allocates a TTY so htop can draw; fall back to top if missing.
            cmd += ["-t", "command -v htop >/dev/null 2>&1 && exec htop || exec top"]
        print(f"$ {' '.join(shlex.quote(c) for c in cmd)}")
        os.execvp(cmd[0], cmd)


def aws_setup() -> None:
    """Interactive setup, run in a Terminal from the menu or by hand."""
    existing = load_aws_config()
    print("Baseten Monitor — AWS dev box setup")
    print("=" * 40)
    print(
        "This stores ONLY the profile, region, and which instances to show, in\n"
        f"  {AWS_CONFIG_PATH}  (mode 0600)\n"
        "No credentials are stored. The plugin uses your AWS CLI sign-in.\n"
    )
    if existing["error"]:
        print(f"⚠️  Current config problem: {existing['error']}\n")

    default_profile = existing.get("profile") or os.environ.get("AWS_PROFILE") or "default"
    profile = input(f"AWS profile [{default_profile}]: ").strip() or default_profile
    if not re.fullmatch(r"[A-Za-z0-9._\-]{1,64}", profile):
        print("Profile name has unexpected characters; aborting.")
        return
    region = input(f"AWS region (blank = profile default) [{existing.get('region') or ''}]: ").strip()
    region = region or existing.get("region", "")
    if region and not re.fullmatch(r"[a-z]{2}(-[a-z]+)+-\d", region):
        print("That does not look like an AWS region; aborting.")
        return

    cfg = {"profile": "" if profile == "default" else profile, "region": region}

    print("\nLooking up instances you can see…")
    for attempt in range(2):
        code, stdout, stderr = _run_aws(aws_base_cmd(cfg) + ["ec2", "describe-instances"])
        if code == 0:
            break
        if _looks_like_auth_error(stderr) and attempt == 0:
            login = aws_login_cmd(cfg)
            print(f"Not signed in. Running: {' '.join(login)}")
            subprocess.run(login)
            continue
        print(f"aws failed: {stderr.strip()[-300:]}")
        return

    instances = []
    for res in json.loads(stdout or "{}").get("Reservations", []):
        for inst in res.get("Instances", []):
            box = _instance_from_json(inst)
            if box["state"] != "terminated":
                instances.append(box)
    instances.sort(key=lambda b: (b["name"].lower(), b["id"]))

    if not instances:
        print("No instances visible with this profile/region.")
    else:
        print()
        for i, b in enumerate(instances, 1):
            print(f"  {i:>3}. {b['name']:<32} {b['id']:<21} {b['state']:<9} {b['type']}")
        print()

    current = ", ".join(existing.get("boxes", []))
    print("Enter your dev boxes as numbers from the list, instance IDs, or Name tags,")
    print("separated by commas. Names must be unique in the account to be reliable.")
    raw = input(f"Dev boxes [{current}]: ").strip() or current
    boxes = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if item.isdigit() and 1 <= int(item) <= len(instances):
            boxes.append(instances[int(item) - 1]["id"])
        elif INSTANCE_ID_RE.fullmatch(item) or INSTANCE_NAME_RE.fullmatch(item):
            boxes.append(item)
        else:
            print(f"Skipping invalid entry: {item!r}")
    if not boxes:
        print("No dev boxes selected; nothing written.")
        return

    ssh_user = input(f"SSH username (blank = use ~/.ssh/config) [{existing.get('ssh_user') or ''}]: ").strip()
    ssh_user = ssh_user or existing.get("ssh_user", "")
    if ssh_user and not SSH_USER_RE.fullmatch(ssh_user):
        print("SSH username has unexpected characters; leaving blank.")
        ssh_user = ""

    ssh_key = input(
        f"SSH private key path, e.g. ~/.ssh/my-key.pem (blank = none) [{existing.get('ssh_key') or ''}]: "
    ).strip()
    ssh_key = ssh_key or existing.get("ssh_key", "")
    if ssh_key:
        expanded = os.path.expanduser(ssh_key)
        if not os.path.isfile(expanded):
            print(f"Key file not found: {expanded}; leaving blank.")
            ssh_key = ""
        elif _secure_file_problem(expanded):
            print(f"Key file {_secure_file_problem(expanded)}. Fix with: chmod 600 {shlex.quote(expanded)}")
            print("Leaving blank for now.")
            ssh_key = ""

    lines = [
        "# Baseten Monitor — AWS dev boxes. No credentials belong in this file.",
        "# The plugin signs in via the AWS CLI (aws login / aws sso login).",
        f"AWS_PROFILE={profile}",
    ]
    if region:
        lines.append(f"AWS_REGION={region}")
    lines.append(f"BTMON_DEV_BOXES={','.join(boxes)}")
    if ssh_user:
        lines.append(f"BTMON_SSH_USER={ssh_user}")
    if ssh_key:
        lines.append(f"BTMON_SSH_KEY={ssh_key}")

    os.makedirs(AWS_CONFIG_DIR, mode=0o700, exist_ok=True)
    fd = os.open(AWS_CONFIG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    os.chmod(AWS_CONFIG_PATH, 0o600)

    print(f"\nWrote {AWS_CONFIG_PATH}")
    print("Refreshing SwiftBar…")
    subprocess.run(["/usr/bin/open", "-g", "swiftbar://refreshplugin?name=baseten"], capture_output=True)


def fetch_baseten() -> tuple[str, str]:
    """Run truss and return (raw_output, error_title). error_title is '' on success."""
    try:
        result = subprocess.run(
            ["truss", "train", "view", "--non-interactive"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        raw = result.stdout + result.stderr
        if result.returncode != 0:
            return (
                f"truss exited {result.returncode}\nstderr: {result.stderr[:200]}",
                "BT: err",
            )
        return raw, ""
    except FileNotFoundError:
        return (
            f"truss not found in PATH\nPATH: {os.environ.get('PATH', 'unset')}",
            "BT: no truss",
        )
    except subprocess.TimeoutExpired:
        return "truss timed out after 30s", "BT: timeout"


def main():
    aws_cfg = load_aws_config()

    # Run truss and aws at the same time so the refresh stays quick.
    with ThreadPoolExecutor(max_workers=2) as pool:
        bt_future = pool.submit(fetch_baseten)
        aws_future = (
            pool.submit(fetch_dev_boxes, aws_cfg)
            if aws_cfg["configured"]
            else None
        )
        raw, bt_error = bt_future.result()
        aws_fetched = (
            aws_future.result()
            if aws_future
            else {"boxes": [], "error": "", "auth_error": False}
        )

    aws_title, aws_color = aws_title_fragment(aws_cfg, aws_fetched)

    if bt_error:
        title = bt_error + (f" · {aws_title}" if aws_title else "")
        print(f"{title} | color={aws_color or 'red'}")
        print("---")
        for line in raw.split("\n"):
            print(line)
        print_aws_section(aws_cfg, aws_fetched)
        sys.exit(0)

    projects = parse_projects(raw)
    jobs = parse_active_jobs(raw, projects)

    # Count active
    running = [j for j in jobs if "RUNNING" in j.get("Status", "").upper()]
    created = [j for j in jobs if "CREATED" in j.get("Status", "").upper()]

    # Menu bar title — show GPU count, plus EC2 summary when configured.
    active = running or created
    gpus = total_gpus(active) if active else 0
    if running:
        gpu_str = f" ({gpus} GPUs)" if gpus else ""
        bt_title, bt_color = f"BT: {len(running)} running{gpu_str}", "green"
    elif created:
        gpu_str = f" ({gpus} GPUs)" if gpus else ""
        bt_title, bt_color = f"BT: {len(created)} starting{gpu_str}", "orange"
    elif jobs:
        bt_title, bt_color = f"BT: {len(jobs)} jobs", ""
    else:
        bt_title, bt_color = "BT: idle", ""

    title = bt_title + (f" · {aws_title}" if aws_title else "")
    # An AWS problem is worth surfacing in color when Baseten is quiet.
    color = bt_color or aws_color
    print(f"{title} | color={color}" if color else title)

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

    print_aws_section(aws_cfg, aws_fetched)

    print("---")
    print("Refresh | refresh=true")


AWS_INSTANCE_ACTIONS = {"--aws-start", "--aws-stop", "--aws-ssm", "--aws-ssh", "--aws-htop"}

if __name__ == "__main__":
    args = sys.argv[1:]
    if len(args) == 2 and args[0] == "--open-wandb":
        open_wandb_run(args[1])
    elif len(args) == 2 and args[0] in AWS_INSTANCE_ACTIONS:
        aws_action(args[0], args[1])
    elif args == ["--aws-login"]:
        aws_action("--aws-login", "")
    elif args == ["--aws-setup"]:
        aws_setup()
    else:
        main()
