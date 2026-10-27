# Baseten Job Monitor

A macOS menu bar plugin that monitors your [Baseten](https://www.baseten.co/) training jobs and (optionally) your AWS EC2 dev boxes in real time using [SwiftBar](https://github.com/swiftbar/SwiftBar).

## Features

### Baseten training jobs

- Live job status in your menu bar (refreshes every 1 minute)
- Color-coded status icons (native SF Symbols): green dot for running, orange dotted circle for starting, red triangle for failed, grey check or stop for completed and stopped
- GPU count summary across active jobs
- Launcher and elapsed runtime for each active job
- Click any job to open it in the Baseten dashboard
- Open the corresponding Weights & Biases run directly from the job menu
- Copy job IDs to clipboard
- Browse all training projects

### AWS dev boxes (optional)

- Menu bar summary of how many of your dev boxes are up, e.g. `BT: idle · EC2: 2/2 up`
- Per-box state icon (running, pending, stopping, stopped, impaired status checks), instance type, uptime, public and private IP
- Live usage inside each box's menu, like a tiny `htop`: CPU % with core count and load average, RAM, swap (when in use), disk on `/`, and on GPU boxes each GPU's utilization, VRAM, and temperature from `nvidia-smi`. A compact `CPU 32% · RAM 41% · GPU 87%` summary sits on the box's main line. This is collected over one SSH call per box, in parallel, so a refresh stays at a few seconds.
- Clicking a running box opens an SSH session in Terminal (or an SSM session if the box has no public address). Clicking a stopped box opens it in the AWS console.
- Submenu actions: open `htop` in Terminal, open in the AWS console, connect via SSM or SSH, copy the IP or instance ID, start a stopped box, or stop a running one (with a confirmation dialog)
- A "Sign in" action appears in the menu when your AWS session has expired

## Prerequisites

- macOS
- Python 3.9+ (the macOS system Python works)
- [SwiftBar](https://github.com/swiftbar/SwiftBar)
- [Truss CLI](https://docs.baseten.co/truss/overview) installed and authenticated
- For AWS dev boxes: [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) installed and signed in (`aws login` or `aws sso login`)

## Installation

### 1. Install SwiftBar

```bash
brew install --cask swiftbar
```

On first launch, SwiftBar will ask you to choose a **plugin directory** (e.g. `~/swiftbar-plugins`). Remember this path.

### 2. Install and authenticate the Truss CLI

```bash
pip install truss
truss login
```

Make sure `truss` is on your PATH:

```bash
which truss
```

### 3. Add the plugin

Clone this repo and symlink (or copy) the plugin into your SwiftBar plugins directory:

```bash
git clone https://github.com/ameya005/baseten-job-monitor.git
ln -s "$(pwd)/baseten-job-monitor/baseten.1m.py" ~/swiftbar-plugins/baseten.1m.py
chmod +x ~/swiftbar-plugins/baseten.1m.py
```

### 4. Refresh SwiftBar

Click the SwiftBar icon in the menu bar and select **Refresh All**, or restart SwiftBar. You should see **BT: idle** (or your active jobs) appear in the menu bar.

### 5. (Optional) Add your AWS dev boxes

Click the menu bar item, then **AWS Dev Boxes → Set up AWS dev boxes…**. A Terminal window opens, lists the EC2 instances your AWS sign-in can see, and asks you to pick yours. Or run it by hand:

```bash
python3 ~/swiftbar-plugins/baseten.1m.py --aws-setup
```

That writes `~/.config/baseten-job-monitor/aws.env` (mode `0600`) that looks like this:

```bash
# Baseten Monitor — AWS dev boxes. No credentials belong in this file.
AWS_PROFILE=default
AWS_REGION=us-east-2          # optional; defaults to the profile's region
BTMON_DEV_BOXES=i-0123456789abcdef0,my-box-name   # instance IDs or Name tags
BTMON_SSH_USER=ubuntu         # optional; blank means rely on ~/.ssh/config
BTMON_SSH_KEY=~/.ssh/my-key.pem   # optional; passed to `ssh -i`, must be mode 0600
```

Any of those keys can also be set as environment variables, which take priority over the file.

## How AWS access is kept secure

The plugin is deliberately unable to hold AWS secrets:

- **No credentials are stored or read by the plugin.** Every AWS call shells out to the `aws` CLI, which uses whatever sign-in you already have: an `aws login` / `aws sso login` session, a named profile, or a `credential_process`. With SSO there are no long-lived keys on disk at all. When the session expires the menu shows a **Sign in** action that runs the right login command for your profile.
- **The config file only holds non-secret settings** (profile name, region, which instances are yours, SSH username, and the *path* to your SSH key). It is plain `KEY=VALUE` text that the plugin parses itself; it is never `source`d or executed.
- **The SSH key is never read by the plugin.** Its path is handed to `ssh -i`, and only if the key file is owned by you and not readable by anyone else; otherwise the SSH action falls back to your `~/.ssh/config` and the menu shows a warning.
- **Usage metrics use the same SSH access you already have.** The plugin pipes a short, fixed shell script (read `/proc/stat`, `free`, `df`, and `nvidia-smi` if present) to `sh` on the box in batch mode, so it never prompts for a password and never runs anything from the config file. If SSH fails, the menu just says usage is unavailable.
- **The config file is checked before it is read.** If it is not owned by you or is readable by group/others (anything other than mode `0600`), the plugin refuses to load it and offers a one-click fix.
- **Credential keys are rejected.** If `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`, or `AWS_SECURITY_TOKEN` appear in the file, the plugin refuses to use it and tells you to sign in with the CLI instead. Unknown keys are ignored with a warning.
- **All input is validated.** Instance IDs, Name tags, profile, region, and SSH user are checked against strict patterns, and every command is run as an argument list with no shell, so nothing in the config can inject commands.
- **Stopping a box asks first.** Start is a single click; Stop shows a native confirmation dialog.

The plugin only needs read access to EC2 (`ec2:DescribeInstances`, `ec2:DescribeInstanceStatus`) for monitoring, plus `ec2:StartInstances` / `ec2:StopInstances` if you use those actions, and `ssm:StartSession` for the SSM connect action.

## Customization

### Refresh interval

The filename controls the refresh interval — `1m` means every 1 minute. Rename the file to change it:

| Filename | Interval |
|---|---|
| `baseten.30s.py` | 30 seconds |
| `baseten.1m.py` | 1 minute (default) |
| `baseten.5m.py` | 5 minutes |

### PATH resolution

The plugin automatically adds common tool directories to PATH (`~/.local/bin`, `/opt/homebrew/bin`, `/usr/local/bin`, mise shims). If `truss` or `aws` is installed somewhere else, add its directory to the `_extra_paths` list at the top of the script.

If you manage `truss` with mise and see `No version is set for shim: truss`, pick a global default, for example `mise use -g pipx:truss@0.18.35`.

## License

MIT
