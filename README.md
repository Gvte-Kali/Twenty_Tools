# Twenty_Tools

A dark-themed SSH/SFTP file manager built with Python and Tkinter. Connects to any SSH-capable machine and lets you browse, transfer, and manage files without ever leaving the app.

---

## Download

### 🪟 Windows — standalone executable

No Python required. Download and run directly:

**[⬇ Download Twenty_Tools.exe](https://github.com/Gvte-Kali/Twenty_Tool/raw/refs/heads/main/Twenty_Tools.exe)**

---

### 🐧 Linux — one-liner

Requires Python 3.10+ and `pip install paramiko`.

```bash
pip install paramiko && wget https://raw.githubusercontent.com/Gvte-Kali/Twenty_Tool/refs/heads/main/Twenty_Tools.py -O Twenty_Tools.py && python3 Twenty_Tools.py
```

Or if you prefer `curl`:

```bash
pip install paramiko && curl -L https://raw.githubusercontent.com/Gvte-Kali/Twenty_Tool/refs/heads/main/Twenty_Tools.py -o Twenty_Tools.py && python3 Twenty_Tools.py
```

---

## Features

### Connection
- Password or SSH key authentication
- SUDO / root mode with a dedicated password field
- Connection test (port reachability check)
- Auto-reconnect detection via 20-second heartbeat
- Thread-safe — prevents duplicate connection attempts

### File Browser
- Dual-pane layout (local ↔ remote) with a draggable sash
- Each pane is independently minimizable / restorable
- Editable path bar — type or paste any path and press Enter to navigate
- Show / hide dot-files toggle (hidden files displayed in muted color)
- Horizontal + vertical scrollbars on both panes
- Multi-selection (Shift+click, Ctrl+click)
- Right-click context menu: Rename, Delete, Open/Edit
- Inline text editor for remote files (opens on double-click)
- Side-by-side diff between a local and a remote file
- Folder creation on both sides

### File Transfer
- **Universal transfer engine** — works on any SSH target:
  - SFTP protocol if available (fastest)
  - `base64` encode/decode via shell if SFTP is absent (covers Alpine, Busybox, minimal containers)
  - `dd` via stdin as last resort (very old systems)
- **Checksum verification** with automatic retry (up to 3 attempts):
  - Uses `md5sum`, `sha256sum`, `sha1sum`, or `cksum` — whichever is available
  - Detects corruption and retries silently
- Transfer progress bar with percentage and filename
- Overwrite confirmation before replacing existing files

### SUDO / Root mode
- Separate root session with sudo elevation
- Browse and transfer protected system files
- All shell paths properly quoted with `shlex.quote` to handle spaces and special characters

### Terminal
- **▶ ⌨ Launch Terminal** button opens a native SSH terminal window
  - Windows: Windows Terminal (wt.exe), falls back to cmd.exe
  - Linux: uses `$TERMINAL`, `x-terminal-emulator`, or `xterm`

### UI
- Fullscreen on launch (F11 toggle, Escape to exit)
- Resizable log panel (drag the handle above it)
- Live connection status indicator in the nav bar
- Dark theme throughout

---

## Requirements

Python 3.10+ and one dependency:

```
paramiko
```

Install it with:

```bash
pip install paramiko
```

---

## Run

```bash
python3 Twenty_Tools.py
```

---

## Build a standalone .exe (Windows)

Install PyInstaller:

```bash
pip install pyinstaller
```

Place `Twenty_Tools.py`, `twenty_tools.ico`, and `Twenty_Tools.spec` in the same folder, then run:

```bash
python -m PyInstaller Twenty_Tools.spec
```

The executable will appear in `dist/Twenty_Tools.exe`. It is fully standalone — Python and paramiko are bundled inside.

---

## Project structure

```
Twenty_Tools/
├── Twenty_Tools.py      # Main application
├── twenty_tools.ico     # App icon
├── Twenty_Tools.spec    # PyInstaller spec file
├── requirements.txt     # Python dependencies
├── README.md
└── .gitignore
```

---

## How the transfer engine works

At connect time, `ShellSession` probes the remote host and selects the best available transfer method automatically:

| Remote capability | Transfer method used |
|---|---|
| SFTP subsystem available | Paramiko SFTP (native, fastest) |
| `base64` command available | base64 encode → shell → decode |
| Neither | `dd` via stdin (raw bytes) |

After every transfer, the local and remote checksums are compared. If they differ, the transfer is retried up to 3 times before reporting an error.

---

## Compatibility

| Target | Works |
|---|---|
| Linux (Debian, Ubuntu, Arch, Fedora…) | ✓ |
| Raspberry Pi / DietPi | ✓ |
| Alpine / Busybox containers | ✓ |
| macOS (SSH server enabled) | ✓ |
| Dropbear-based systems (routers, firmwares) | ✓ |
| Windows OpenSSH server | ✓ (SFTP mode) |

---

## License

MIT
