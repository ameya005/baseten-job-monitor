# Baseten Job Monitor

A macOS menu bar plugin that monitors your [Baseten](https://www.baseten.co/) training jobs in real time using [SwiftBar](https://github.com/swiftbar/SwiftBar).

## Features

- Live job status in your menu bar (refreshes every 1 minute)
- Color-coded status indicators:
  - 🟢 Running
  - 🟡 Starting / Created
  - 🔴 Failed
  - ⚫ Stopped / Completed
- GPU count summary across active jobs
- Launcher and elapsed runtime for each active job
- Click any job to open it in the Baseten dashboard
- Open the corresponding Weights & Biases run directly from the job menu
- Copy job IDs to clipboard
- Browse all training projects

## Prerequisites

- macOS
- Python 3.10+
- [SwiftBar](https://github.com/swiftbar/SwiftBar)
- [Truss CLI](https://docs.baseten.co/truss/overview) installed and authenticated

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
git clone https://github.com/ameya005/swiftbar-plugins.git
ln -s "$(pwd)/swiftbar-plugins/baseten.1m.py" ~/swiftbar-plugins/baseten.1m.py
chmod +x ~/swiftbar-plugins/baseten.1m.py
```

### 4. Refresh SwiftBar

Click the SwiftBar icon in the menu bar and select **Refresh All**, or restart SwiftBar. You should see **BT: idle** (or your active jobs) appear in the menu bar.

## Customization

### Refresh interval

The filename controls the refresh interval — `1m` means every 1 minute. Rename the file to change it:

| Filename | Interval |
|---|---|
| `baseten.30s.py` | 30 seconds |
| `baseten.1m.py` | 1 minute (default) |
| `baseten.5m.py` | 5 minutes |

### PATH resolution

The plugin automatically adds common tool directories to PATH (`~/.local/bin`, `/opt/homebrew/bin`, `/usr/local/bin`, mise shims). If `truss` is installed somewhere else, add its directory to the `_extra_paths` list at the top of the script.

## License

MIT
