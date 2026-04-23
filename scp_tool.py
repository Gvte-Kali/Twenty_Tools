#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import threading
import os
import stat
import json
import socket
import time
import platform
import base64
import shutil
import re
import shlex

IS_WINDOWS = platform.system() == "Windows"
IS_LINUX   = platform.system() == "Linux"

try:
    import paramiko
    from paramiko import SSHClient, AutoAddPolicy
    PARAMIKO_OK = True
except ImportError:
    PARAMIKO_OK = False

# ── Color palette ─────────────────────────────────────────────────────────────
BG      = "#12141c"
PANEL   = "#1e2130"
PANEL2  = "#252840"
ACCENT  = "#2ecc9a"
ACCENT2 = "#4d9eff"
TEXT    = "#f0f2ff"
TEXT2   = "#c8cde8"
MUTED   = "#7880a8"
ERROR   = "#ff5f7e"
SUCCESS = "#2ecc9a"
BORDER  = "#333750"
SEL_BG  = "#2a4a7f"
WARN    = "#f0a030"
SUDO_ON = "#c84bff"   # vivid purple – impossible to miss

# ── Fonts ─────────────────────────────────────────────────────────────────────
FT         = ("Segoe UI", 10)
FT_BOLD    = ("Segoe UI", 10, "bold")
FT_MONO    = ("Consolas", 10)
FT_MONO_SM = ("Consolas", 9)
FT_BTN     = ("Segoe UI", 10, "bold")
FT_SMALL   = ("Segoe UI", 8)
FT_LABEL   = ("Segoe UI", 9)

PROFILES_FILE = os.path.join(os.path.expanduser("~"), ".twenty_tools_profiles.json")


# ── SSH helpers ───────────────────────────────────────────────────────────────
def _ssh_connect(ip, port, user, pwd, key_path=None):
    ssh = SSHClient()
    ssh.set_missing_host_key_policy(AutoAddPolicy())
    if key_path and os.path.isfile(key_path):
        ssh.connect(ip, port=port, username=user,
                    key_filename=key_path, timeout=10)
    else:
        ssh.connect(ip, port=port, username=user, password=pwd, timeout=10)
    return ssh


def _ping_port(ip, port, timeout=3):
    try:
        s = socket.create_connection((ip, int(port)), timeout=timeout)
        s.close()
        return True
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════════════════════
# ShellSession — unified SSH session that works with or without SFTP
#
# Capabilities are detected at connect time:
#   self.has_sftp   — paramiko SFTP channel available
#   self.has_base64 — remote `base64` command available
#   self.checksum   — "md5sum" | "sha256sum" | "cksum" | None
#
# All transfers verify integrity with checksum + retry up to MAX_RETRIES times.
# ══════════════════════════════════════════════════════════════════════════════
MAX_RETRIES   = 3
CHUNK_SIZE    = 32 * 1024   # 32 KB chunks for base64 transfer


class ShellSession:
    """Wraps a paramiko SSHClient and exposes a unified file-transfer API."""

    def __init__(self, ssh: "SSHClient", log_fn=None):
        self.ssh      = ssh
        self._log     = log_fn or (lambda msg, col=None: None)
        self.has_sftp = False
        self.sftp     = None
        self.cwd      = "/"

        # Detect capabilities
        self.has_base64 = self._probe("command -v base64 >/dev/null 2>&1")
        self.checksum   = self._detect_checksum()
        # Try opening SFTP
        try:
            self.sftp     = ssh.open_sftp()
            self.has_sftp = True
            try:
                self.cwd = self.sftp.normalize(".")
            except Exception:
                self.cwd = self._shell_cwd()
            self._log("[Connection] SFTP available ✓", SUCCESS)
        except Exception:
            self.cwd = self._shell_cwd()
            self._log("[Connection] SFTP unavailable — shell-only mode", WARN)

    # ── Internal helpers ──────────────────────────────────────────────────
    def _run(self, cmd: str, timeout: int = 30) -> tuple[str, str, int]:
        """Run a shell command, return (stdout, stderr, exit_code)."""
        _, out, err = self.ssh.exec_command(cmd, timeout=timeout)
        rc  = out.channel.recv_exit_status()
        return out.read().decode("utf-8", errors="replace"),                err.read().decode("utf-8", errors="replace").strip(), rc

    def _probe(self, cmd: str) -> bool:
        """Return True if cmd exits with code 0."""
        try:
            _, _, rc = self._run(cmd, timeout=5)
            return rc == 0
        except Exception:
            return False

    def _detect_checksum(self) -> str | None:
        """Return the best available checksum command on the remote."""
        for cmd in ("md5sum", "sha256sum", "sha1sum", "cksum"):
            if self._probe(f"command -v {cmd} >/dev/null 2>&1"):
                return cmd
        return None

    def _shell_cwd(self) -> str:
        out, _, _ = self._run("pwd")
        return out.strip() or "/"

    def _remote_checksum(self, remote_path: str) -> str | None:
        """Compute checksum of a remote file. Returns hex string or None."""
        if not self.checksum:
            return None
        out, _, rc = self._run(
            f"{self.checksum} {shlex.quote(remote_path)}", timeout=60)
        if rc != 0:
            return None
        return out.strip().split()[0] if out.strip() else None

    def _local_checksum(self, local_path: str) -> str | None:
        """Compute checksum of a local file using the same algorithm."""
        import hashlib
        algo = {
            "md5sum":    "md5",
            "sha256sum": "sha256",
            "sha1sum":   "sha1",
        }.get(self.checksum)
        if not algo and self.checksum == "cksum":
            # cksum is CRC32 — use binascii
            import binascii
            try:
                with open(local_path, "rb") as f:
                    crc = 0
                    while chunk := f.read(65536):
                        crc = binascii.crc32(chunk, crc)
                return str(crc & 0xFFFFFFFF)
            except Exception:
                return None
        if not algo:
            return None
        try:
            h = hashlib.new(algo)
            with open(local_path, "rb") as f:
                while chunk := f.read(65536):
                    h.update(chunk)
            return h.hexdigest()
        except Exception:
            return None

    # ── Directory listing ─────────────────────────────────────────────────
    def listdir(self, path: str) -> list[tuple[str, bool, bool]]:
        """
        Return [(name, is_dir, is_readable), ...] sorted dirs-first.
        Works via SFTP if available, else via `ls`.
        """
        if self.has_sftp:
            try:
                entries = self.sftp.listdir_attr(path)
                result = []
                for e in sorted(entries, key=lambda x: x.filename):
                    is_dir = stat.S_ISDIR(e.st_mode)
                    readable = bool(e.st_mode & 0o444)
                    result.append((e.filename, is_dir, readable))
                return sorted(result, key=lambda x: (not x[1], x[0]))
            except Exception:
                pass   # fall through to shell

        # Shell fallback
        out, _, rc = self._run(
            f"ls -1p --color=never {shlex.quote(path)} 2>/dev/null")
        if rc != 0:
            # Try find as last resort
            out, _, _ = self._run(
                f"find {shlex.quote(path)} -maxdepth 1 -printf '%f\t%y\n' 2>/dev/null")
            result = []
            for line in out.splitlines():
                parts = line.split("\t")
                if len(parts) == 2 and parts[0] not in (".", ""):
                    result.append((parts[0], parts[1] == "d", True))
            return sorted(result, key=lambda x: (not x[1], x[0]))

        result = []
        for name in out.splitlines():
            if not name or name in (".", ".."):
                continue
            is_dir = name.endswith("/")
            result.append((name.rstrip("/"), is_dir, True))
        return sorted(result, key=lambda x: (not x[1], x[0]))

    def makedirs(self, path: str):
        if self.has_sftp:
            try:
                self.sftp.mkdir(path); return
            except Exception: pass
        self._run(f"mkdir -p {shlex.quote(path)}")

    def rename(self, old: str, new: str):
        if self.has_sftp:
            try:
                self.sftp.rename(old, new); return
            except Exception: pass
        self._run(f"mv {shlex.quote(old)} {shlex.quote(new)}")

    def remove(self, path: str, is_dir=False):
        if self.has_sftp:
            try:
                if is_dir: self.sftp.rmdir(path)
                else:      self.sftp.remove(path)
                return
            except Exception: pass
        cmd = f"rm -rf {shlex.quote(path)}" if is_dir else f"rm -f {shlex.quote(path)}"
        self._run(cmd)

    def stat_exists(self, path: str) -> bool:
        if self.has_sftp:
            try:
                self.sftp.stat(path); return True
            except Exception: return False
        _, _, rc = self._run(f"test -e {shlex.quote(path)}")
        return rc == 0

    def read_text(self, path: str) -> str:
        """Read a remote text file and return its content."""
        if self.has_sftp:
            with self.sftp.open(path, "r") as f:
                return f.read().decode(errors="replace")
        out, _, _ = self._run(f"cat {shlex.quote(path)}", timeout=30)
        return out

    def write_text(self, path: str, text: str):
        """Write text content to a remote file."""
        if self.has_sftp:
            with self.sftp.open(path, "w") as f:
                f.write(text.encode()); return
        # Shell fallback via base64
        encoded = base64.b64encode(text.encode()).decode()
        self._run(
            f"echo {shlex.quote(encoded)} | base64 -d > {shlex.quote(path)}")

    # ── Upload with checksum + retry ──────────────────────────────────────
    def upload(self, local_path: str, remote_path: str,
               progress_cb=None) -> bool:
        """
        Upload local_path → remote_path.
        progress_cb(done_bytes, total_bytes) called periodically.
        Returns True on success (checksum verified).
        """
        file_size = os.path.getsize(local_path)
        local_cksum = self._local_checksum(local_path)

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                if self.has_sftp:
                    self._upload_sftp(local_path, remote_path,
                                      file_size, progress_cb)
                elif self.has_base64:
                    self._upload_base64(local_path, remote_path,
                                        file_size, progress_cb)
                else:
                    self._upload_dd(local_path, remote_path,
                                    file_size, progress_cb)

                # Verify checksum
                if local_cksum and self.checksum:
                    remote_cksum = self._remote_checksum(remote_path)
                    if remote_cksum and remote_cksum != local_cksum:
                        self._log(
                            f"[Transfer] Checksum mismatch attempt {attempt}"
                            f" — retrying...", WARN)
                        continue   # retry
                    elif remote_cksum == local_cksum:
                        self._log(f"[Transfer] ✓ Checksum OK", SUCCESS)

                return True   # success (or no checksum available)

            except Exception as e:
                self._log(
                    f"[Transfer] Upload error attempt {attempt}: {e}", WARN)
                if attempt == MAX_RETRIES:
                    raise

        self._log(
            f"[Transfer] ✗ Upload failed after {MAX_RETRIES} attempts", ERROR)
        return False

    def _upload_sftp(self, lp, rp, size, cb):
        def _wrap(done, _total):
            if cb: cb(done, size)
        self.sftp.put(lp, rp, callback=_wrap)

    def _upload_base64(self, lp, rp, size, cb):
        """Encode file in base64 chunks and stream via shell."""
        with open(lp, "rb") as f:
            data = f.read()
        encoded = base64.b64encode(data).decode()
        # Send in one shot via here-string — works up to ~50MB reliably
        # For larger files, chunk it
        if len(data) <= 10 * 1024 * 1024:   # ≤ 10MB: single shot
            _, err, rc = self._run(
                f"echo {shlex.quote(encoded)} | base64 -d > {shlex.quote(rp)}",
                timeout=120)
            if rc != 0:
                raise RuntimeError(f"base64 upload failed: {err}")
        else:
            # Chunk: write first chunk, then append rest
            chunk_b64_size = 65536   # base64 chars per chunk (~48KB decoded)
            first = True
            done  = 0
            for i in range(0, len(encoded), chunk_b64_size):
                chunk  = encoded[i:i + chunk_b64_size]
                op     = ">" if first else ">>"
                _, err, rc = self._run(
                    f"echo {shlex.quote(chunk)} | base64 -d {op} {shlex.quote(rp)}",
                    timeout=60)
                if rc != 0:
                    raise RuntimeError(f"base64 chunk upload failed: {err}")
                first = False
                done += len(chunk) * 3 // 4   # approx decoded bytes
                if cb: cb(min(done, size), size)

        if cb: cb(size, size)

    def _upload_dd(self, lp, rp, size, cb):
        """Fallback: pipe raw bytes via stdin to dd."""
        with open(lp, "rb") as f:
            data = f.read()
        _, stdin, _ = self.ssh.exec_command(
            f"dd of={shlex.quote(rp)} bs=32768 2>/dev/null")
        done = 0
        for i in range(0, len(data), CHUNK_SIZE):
            chunk = data[i:i + CHUNK_SIZE]
            stdin.write(chunk)
            done += len(chunk)
            if cb: cb(done, size)
        stdin.channel.shutdown_write()
        if cb: cb(size, size)

    # ── Download with checksum + retry ────────────────────────────────────
    def download(self, remote_path: str, local_path: str,
                 progress_cb=None) -> bool:
        """
        Download remote_path → local_path.
        Returns True on success (checksum verified).
        """
        # Get remote checksum before transfer
        remote_cksum = self._remote_checksum(remote_path)

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                if self.has_sftp:
                    self._download_sftp(remote_path, local_path,
                                        progress_cb)
                elif self.has_base64:
                    self._download_base64(remote_path, local_path,
                                          progress_cb)
                else:
                    self._download_cat(remote_path, local_path,
                                       progress_cb)

                # Verify
                if remote_cksum and self.checksum:
                    local_cksum = self._local_checksum(local_path)
                    if local_cksum and local_cksum != remote_cksum:
                        self._log(
                            f"[Transfer] Checksum mismatch attempt {attempt}"
                            f" — retrying...", WARN)
                        continue
                    elif local_cksum == remote_cksum:
                        self._log(f"[Transfer] ✓ Checksum OK", SUCCESS)

                return True

            except Exception as e:
                self._log(
                    f"[Transfer] Download error attempt {attempt}: {e}", WARN)
                if attempt == MAX_RETRIES:
                    raise

        self._log(
            f"[Transfer] ✗ Download failed after {MAX_RETRIES} attempts",
            ERROR)
        return False

    def _download_sftp(self, rp, lp, cb):
        attr = self.sftp.stat(rp)
        size = attr.st_size or 1
        def _wrap(done, _total):
            if cb: cb(done, size)
        self.sftp.get(rp, lp, callback=_wrap)

    def _download_base64(self, rp, lp, cb):
        out, err, rc = self._run(
            f"base64 {shlex.quote(rp)}", timeout=300)
        if rc != 0:
            raise RuntimeError(f"base64 download failed: {err}")
        data = base64.b64decode(out.strip())
        with open(lp, "wb") as f:
            f.write(data)
        if cb: cb(len(data), len(data))

    def _download_cat(self, rp, lp, cb):
        """Last resort: cat via exec_command stdout."""
        _, out, _ = self.ssh.exec_command(
            f"cat {shlex.quote(rp)}", timeout=300)
        data = out.read()
        with open(lp, "wb") as f:
            f.write(data)
        if cb: cb(len(data), len(data))

    def close(self):
        if self.sftp:
            try: self.sftp.close()
            except Exception: pass
        try: self.ssh.close()
        except Exception: pass


# ══════════════════════════════════════════════════════════════════════════════
class TwentyTools(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("Twenty_Tools  ·  SSH on port 22")
        self.configure(bg=BG)
        self.resizable(True, True)
        self.minsize(900, 600)
        self._set_icon()
        self._go_fullscreen()

        # ── Persistent data ───────────────────────────────────────────────
        self.profiles   = {}  # profiles removed

        # ── Sudo mode flag ────────────────────────────────────────────────
        self._sudo_mode = False

        # ── Active sessions ───────────────────────────────────────────────
        # Normal session (ShellSession — SFTP or shell-only, auto-detected)
        self.session        = None   # ShellSession instance
        self.session_ssh    = None   # underlying paramiko SSHClient
        self.session_cwd    = "/"
        self.session_local  = os.path.expanduser("~")

        # Sudo/root session (ShellSession with sudo elevation)
        self.root_session   = None   # ShellSession instance
        self.root_ssh       = None
        self.root_sudo_pwd  = None
        self.root_cwd       = "/"
        self.root_local_cwd = os.path.expanduser("~")

        # ── Compat aliases (keep old names working in UI code) ────────────
        self.sftp_conn      = None   # set to session when connected
        self.sftp_ssh       = None
        self.sftp_cwd       = "/"
        self.sftp_local_cwd = os.path.expanduser("~")

        # ── Terminal state ────────────────────────────────────────────────
        self.term_ssh     = None
        self.term_channel = None
        self.term_running = False

        # ── UI flags ──────────────────────────────────────────────────────
        self._connecting     = False   # prevents duplicate connect threads
        self._show_hidden    = False   # toggle hidden files (dot-files)
        self._heartbeat_id   = None    # after() id for connection heartbeat

        self._build_ui()
        self.protocol('WM_DELETE_WINDOW', self._on_close)

        if not PARAMIKO_OK:
            self._log("  paramiko not installed — run: pip install paramiko", ERROR)

    # ══════════════════════════════════════════════════════════════════════
    # FULLSCREEN
    # ══════════════════════════════════════════════════════════════════════
    def _go_fullscreen(self):
        """Launch maximized. F11 toggles, Escape exits."""
        if IS_WINDOWS:
            self.after(0, lambda: self.state("zoomed"))
        else:
            self.after(0, lambda: self.attributes("-zoomed", True))
        self.bind("<F11>",    lambda e: self._toggle_fullscreen())
        self.bind("<Escape>", lambda e: self._exit_fullscreen())

    def _toggle_fullscreen(self):
        if IS_WINDOWS:
            self.state("normal" if self.state() == "zoomed" else "zoomed")
        else:
            self.attributes("-zoomed", not self.attributes("-zoomed"))

    def _exit_fullscreen(self):
        if IS_WINDOWS: self.state("normal")
        else:          self.attributes("-zoomed", False)

    # ══════════════════════════════════════════════════════════════════════
    # ICON  (base64-embedded, no external file needed)
    # ══════════════════════════════════════════════════════════════════════
    def _on_close(self):
        """Clean shutdown — close all SSH connections before destroying window."""
        self._connecting = False
        # Stop heartbeat
        if self._heartbeat_id:
            self.after_cancel(self._heartbeat_id)
        # Close all sessions
        for sess in (getattr(self, 'session', None),
                     getattr(self, 'root_session', None)):
            if sess:
                try: sess.close()
                except Exception: pass
        self.destroy()

    def _set_icon(self):
        try:
            import tempfile
            ico_b64 = (
                "AAABAAEAEBAAAAAAIABVAQAAFgAAAIlQTkcNChoKAAAADUlIRFIAAAAQAAAAEAgGAAAAH/P/YQAAARxJREFU"
                "eJxjZGBgYFDVcPjPQAa4feMAI5OqhsP/W+ZicMFbU9/B2UwL8ZurquHwn/F/fBhCVdwNOFODRxDO/hfP"
                "iNMQJrWTrxgYGBgY1E6+YlDLFoKws4XgmvBpZmBgYGAk1/9wA4REZP4zMDAwcDnpMgiVBTL8//OX4deN"
                "pwzv2tcysOsrYIj9ffsZuwFwAU42BrHeRIaflx4wfJixE6cYDDChO+n/918M///8Zfj/9x9eMZwGcLsb"
                "MLDKijB83XwarxhWA9i0ZRkEMj0Z3ratZfjz4gNOMawGsEgIMIjUhjG8n7KV4cfp2zjF0AE8EPli7Bn4"
                "YuzhEt+PXGf4/eAVhtjbltXYDSAXYAQi/Q149+YJ/sSOB7x784QRAOwngppVCywuAAAAAElFTkSuQmCC"
            )
            ico_bytes = base64.b64decode(ico_b64)
            tmp = tempfile.NamedTemporaryFile(suffix=".ico", delete=False)
            tmp.write(ico_bytes); tmp.close()
            if IS_WINDOWS:
                self.iconbitmap(tmp.name)
            else:
                img = tk.PhotoImage(file=tmp.name)
                self.iconphoto(True, img)
            os.unlink(tmp.name)
        except Exception:
            pass

    # ══════════════════════════════════════════════════════════════════════
    # JSON / PROFILE HELPERS
    # ══════════════════════════════════════════════════════════════════════
    def _load_json(self, path, default):
        if os.path.isfile(path):
            try:
                with open(path) as f:
                    return json.load(f)
            except Exception:
                pass
        return default

    def _save_json(self, path, data):
        try:
            with open(path, "w") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

    def _load_profiles(self):
        if os.path.isfile(PROFILES_FILE):
            try:
                with open(PROFILES_FILE) as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save_profiles(self):
        try:
            with open(PROFILES_FILE, "w") as f:
                json.dump(self.profiles, f, indent=2)
        except Exception as e:
            self._log(f"[Profile] Could not save: {e}", ERROR)

    def _refresh_profile_list(self):
        self.profile_combo["values"] = list(self.profiles.keys())

    def _save_profile(self):
        name = self._ask_string("Save Profile", "Profile name:")
        if not name:
            return
        PLACEHOLDERS = {"IP / Hostname", "Username", "Password", "22"}
        ip   = self.ip_var.get().strip()
        port = self.port_var.get().strip()
        user = self.user_var.get().strip()
        pwd  = self.pass_var.get()
        self.profiles[name] = {
            "ip":       "" if ip   in PLACEHOLDERS else ip,
            "port":     "22" if port in PLACEHOLDERS else port,
            "user":     "" if user in PLACEHOLDERS else user,
            "password": "" if pwd  in PLACEHOLDERS else pwd,
            "key":      self.key_var.get().strip(),
            "auth":     self.auth_mode.get(),
        }
        self._save_profiles()
        self._refresh_profile_list()
        self.profile_var.set(name)
        self._log(f"[Profile] '{name}' saved", SUCCESS)

    def _load_profile(self, _e=None):
        name = self.profile_var.get()
        if name not in self.profiles:
            return
        p = self.profiles[name]
        self.ip_var.set(p.get("ip", ""))
        self.port_var.set(p.get("port", "22"))
        self.user_var.set(p.get("user", ""))
        self.pass_var.set(p.get("password", ""))
        self.key_var.set(p.get("key", ""))
        self.auth_mode.set(p.get("auth", "password"))
        self._toggle_auth()
        # Refresh visual appearance of nav entries (clear placeholder style)
        if hasattr(self, "_ip_entry"):
            self._ip_entry.config(fg=TEXT if p.get("ip") else MUTED)
            self._pass_entry.config(fg=TEXT if p.get("password") else MUTED)
        self._log(f"[Profile] '{name}' loaded", ACCENT2)

    def _delete_profile(self):
        name = self.profile_var.get()
        if not name or name not in self.profiles:
            return
        if messagebox.askyesno("Confirm", f"Delete profile '{name}'?"):
            del self.profiles[name]
            self._save_profiles()
            self._refresh_profile_list()
            self.profile_var.set("")
            self._log(f"[Profile] '{name}' deleted", MUTED)

    # ══════════════════════════════════════════════════════════════════════
    # MAIN UI
    # ══════════════════════════════════════════════════════════════════════
    def _build_ui(self):
        style = ttk.Style()
        style.theme_use("default")
        style.configure("TCombobox",
                        fieldbackground="#0d0f18", background="#0c0e15",
                        foreground=TEXT, selectbackground=SEL_BG,
                        selectforeground=TEXT, arrowcolor=MUTED)

        # ── Inline helper: compact nav entry ──────────────────────────────
        def _nav_entry(parent, placeholder, width, show=None):
            """Small borderless entry for the nav bar."""
            var = tk.StringVar()
            e = tk.Entry(parent, textvariable=var, width=width,
                         font=("Segoe UI", 9),
                         bg="#1a1d27", fg=TEXT2,
                         insertbackground=ACCENT,
                         bd=0, highlightthickness=1,
                         highlightbackground="#2a2d3a",
                         highlightcolor=ACCENT,
                         relief="flat")
            if show:
                e.config(show=show)
            # Placeholder behaviour
            def _on_focus_in(_):
                if e.get() == placeholder:
                    e.delete(0, "end")
                    e.config(fg=TEXT)
            def _on_focus_out(_):
                if not e.get():
                    e.insert(0, placeholder)
                    e.config(fg=MUTED)
            e.insert(0, placeholder)
            e.config(fg=MUTED)
            e.bind("<FocusIn>",  _on_focus_in)
            e.bind("<FocusOut>", _on_focus_out)
            e.pack(side="left", padx=(0, 4), ipady=4)
            return var, e

        def _vsep(parent):
            tk.Frame(parent, bg=BORDER, width=1).pack(
                side="left", fill="y", pady=6, padx=(4, 4))

        # ══════════════════════════════════════════════════════════════════
        # NAV ROW 1 — Connection details + Sudo
        # ══════════════════════════════════════════════════════════════════
        nav1 = tk.Frame(self, bg="#0c0e15")
        nav1.pack(fill="x")

        # Logo
        tk.Label(nav1, text="⚡", font=("Segoe UI", 11, "bold"),
                 bg="#0c0e15", fg=ACCENT).pack(side="left", padx=(12, 4), pady=7)
        tk.Label(nav1, text="Twenty_Tools", font=("Segoe UI", 9, "bold"),
                 bg="#0c0e15", fg=TEXT2).pack(side="left", padx=(0, 2))

        _vsep(nav1)

        # IP / Port
        self.ip_var, self._ip_entry = _nav_entry(nav1, "IP / Hostname", 16)
        self.port_var, _            = _nav_entry(nav1, "22", 4)

        _vsep(nav1)

        # Username
        self.user_var, _ = _nav_entry(nav1, "Username", 10)

        # Fixed-width slot — password and key swap in place, nav width never shifts
        auth_slot = tk.Frame(nav1, bg="#0c0e15", width=190, height=28)
        auth_slot.pack(side="left", padx=(0, 4))
        auth_slot.pack_propagate(False)

        # Password entry (visible by default)
        self.pass_var = tk.StringVar()
        self._pass_entry = tk.Entry(
            auth_slot, textvariable=self.pass_var,
            show="*", font=("Segoe UI", 9),
            bg="#1a1d27", fg=MUTED, insertbackground=ACCENT,
            bd=0, highlightthickness=1,
            highlightbackground="#2a2d3a", highlightcolor=ACCENT,
            relief="flat")
        self._pass_entry.place(relx=0, rely=0.5, anchor="w",
                               relwidth=1.0, height=24)
        def _pwd_focus_in(_):
            if self.pass_var.get() == "Password":
                self.pass_var.set("")
                self._pass_entry.config(fg=TEXT, show="*")
        def _pwd_focus_out(_):
            if not self.pass_var.get():
                self.pass_var.set("Password")
                self._pass_entry.config(fg=MUTED, show="")
        self._pass_entry.insert(0, "Password")
        self._pass_entry.config(show="")
        self._pass_entry.bind("<FocusIn>",  _pwd_focus_in)
        self._pass_entry.bind("<FocusOut>", _pwd_focus_out)

        # Key frame — same slot, hidden by default
        self.key_var = tk.StringVar()
        self._key_entry_frame = tk.Frame(auth_slot, bg="#0c0e15")
        key_inner = tk.Frame(self._key_entry_frame, bg="#0c0e15")
        key_inner.pack(fill="both", expand=True)
        self._key_entry = tk.Entry(
            key_inner, textvariable=self.key_var,
            font=("Segoe UI", 9),
            bg="#1a1d27", fg=TEXT2, insertbackground=ACCENT,
            bd=0, highlightthickness=1,
            highlightbackground="#2a2d3a", highlightcolor=ACCENT,
            relief="flat")
        self._key_entry.pack(side="left", fill="both", expand=True, ipady=4)
        tk.Button(key_inner, text="…", font=FT_SMALL,
                  bg="#1a1d27", fg=TEXT2, bd=0, cursor="hand2", padx=5,
                  command=self._browse_key).pack(side="left")

        # Auth radio (pwd / key)
        self.auth_mode = tk.StringVar(value="password")
        auth_f = tk.Frame(nav1, bg="#0c0e15")
        auth_f.pack(side="left", padx=(0, 6))
        tk.Radiobutton(auth_f, text="pwd", variable=self.auth_mode,
                       value="password", font=("Segoe UI", 8),
                       bg="#0c0e15", fg=MUTED, activebackground="#0c0e15",
                       selectcolor="#0c0e15", bd=0,
                       command=self._toggle_auth).pack(side="left")
        tk.Radiobutton(auth_f, text="key", variable=self.auth_mode,
                       value="key", font=("Segoe UI", 8),
                       bg="#0c0e15", fg=MUTED, activebackground="#0c0e15",
                       selectcolor="#0c0e15", bd=0,
                       command=self._toggle_auth).pack(side="left")

        _vsep(nav1)

        # SUDO toggle button
        self._sudo_btn = tk.Button(
            nav1, text="⚡ SUDO",
            font=("Segoe UI", 9, "bold"),
            bg="#0c0e15", fg=MUTED,
            activebackground="#1a0d2e", activeforeground=SUDO_ON,
            bd=0, cursor="hand2", padx=8, pady=7,
            command=self._toggle_sudo)
        self._sudo_btn.pack(side="left", padx=(0, 4))

        # Sudo password field — inline in row 1, hidden until SUDO ON
        self._sudo_pwd_row = tk.Frame(nav1, bg="#0c0e15")
        _sp = tk.Frame(self._sudo_pwd_row, bg="#0c0e15")
        _sp.pack(fill="y", expand=True, padx=(0, 8), pady=3)
        tk.Label(_sp, text="🔑", font=("Segoe UI", 9),
                 bg="#0c0e15", fg=SUDO_ON).pack(side="left", padx=(0, 4))
        self.sudo_pwd_var = tk.StringVar()
        tk.Entry(_sp, textvariable=self.sudo_pwd_var,
                 show="*", font=("Segoe UI", 9), width=16,
                 bg="#1a1d27", fg=TEXT, insertbackground=ACCENT, bd=0,
                 highlightthickness=1, highlightbackground=SUDO_ON,
                 highlightcolor=ACCENT, relief="flat"
                 ).pack(side="left", ipady=4)
        tk.Label(_sp, text="(blank = reuse pwd)",
                 font=("Segoe UI", 7), bg="#0c0e15",
                 fg=MUTED).pack(side="left", padx=(6, 0))
        # NOT packed yet — _toggle_sudo shows/hides it

        # Row 1 separator
        tk.Frame(self, bg="#1a1d2a", height=1).pack(fill="x")

        # ══════════════════════════════════════════════════════════════════
        # NAV ROW 2 — Actions
        # ══════════════════════════════════════════════════════════════════
        nav2 = tk.Frame(self, bg="#080a12")
        nav2.pack(fill="x")

        # Connect
        tk.Button(nav2, text="▶  Connect", font=("Segoe UI", 10, "bold"),
                  bg=ACCENT, fg="#0a1510",
                  activebackground="#27b589", activeforeground="#0a1510",
                  bd=0, cursor="hand2", padx=16, pady=7,
                  command=self._connect_active).pack(side="left", padx=(12, 4), pady=3)

        # Disconnect
        tk.Button(nav2, text="Disconnect", font=("Segoe UI", 10),
                  bg=PANEL2, fg=ERROR,
                  activebackground=PANEL, activeforeground=ERROR,
                  bd=0, cursor="hand2", padx=12, pady=7,
                  command=self._disconnect_active).pack(side="left", padx=(0, 4), pady=3)

        _vsep(nav2)

        # Test connection
        tk.Button(nav2, text="⬡  Test connection", font=("Segoe UI", 10),
                  bg=PANEL2, fg=ACCENT2,
                  activebackground=PANEL, activeforeground=ACCENT2,
                  bd=0, cursor="hand2", padx=12, pady=7,
                  command=self._ping).pack(side="left", padx=(4, 4), pady=3)
        self.ping_lbl = tk.Label(nav2, text="", font=("Segoe UI", 9),
                                 bg="#080a12", fg=MUTED)
        self.ping_lbl.pack(side="left", padx=(0, 8))

        # Status dot
        self._conn_dot = tk.Label(nav2, text="●", font=("Segoe UI", 11),
                                  bg="#080a12", fg=BORDER)
        self._conn_dot.pack(side="left", padx=(0, 3))
        self.conn_status_bar = tk.Label(nav2, text="not connected",
                                        font=("Segoe UI", 9),
                                        bg="#080a12", fg=MUTED)
        self.conn_status_bar.pack(side="left", padx=(0, 8))

        # Launch Terminal — far right, prominent green
        tk.Button(nav2, text="▶  ⌨  Launch Terminal",
                  font=("Segoe UI", 10, "bold"),
                  bg=ACCENT, fg="#0a1510",
                  activebackground="#27b589", activeforeground="#0a1510",
                  bd=0, cursor="hand2", padx=16, pady=7,
                  command=self._term_launch).pack(side="right", padx=(0, 12), pady=3)

        # ── Accent line ────────────────────────────────────────────────────
        self._accent_line = tk.Frame(self, bg=ACCENT, height=2)
        self._accent_line.pack(fill="x")

        # Compat stubs
        self._conn_collapsed = False
        self._conn_act_row   = nav1
        self.conn_body       = nav1

        # ── BROWSER ────────────────────────────────────────────────────────
        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=12, pady=(6, 0))
        self._build_browser(body)

        # ── LOG  (resizable via drag handle) ──────────────────────────────
        # Drag handle — a thin bar the user can drag up/down to resize the log
        self._log_drag_handle = tk.Frame(self, bg=BORDER, height=4,
                                         cursor="sb_v_double_arrow")
        self._log_drag_handle.pack(fill="x", padx=18, pady=(4, 0))
        self._log_drag_handle.bind("<ButtonPress-1>",   self._log_drag_start)
        self._log_drag_handle.bind("<B1-Motion>",       self._log_drag_motion)
        self._log_drag_handle.bind("<Enter>",
            lambda e: self._log_drag_handle.config(bg=ACCENT2))
        self._log_drag_handle.bind("<Leave>",
            lambda e: self._log_drag_handle.config(bg=BORDER))

        log_wrap = tk.Frame(self, bg=BG)
        log_wrap.pack(fill="x", padx=18, pady=(0, 8))

        lh = tk.Frame(log_wrap, bg=BG)
        lh.pack(fill="x")
        tk.Label(lh, text="LOG", font=("Segoe UI", 8, "bold"),
                 bg=BG, fg=MUTED).pack(side="left", pady=(0, 2))
        tk.Button(lh, text="Clear", font=("Segoe UI", 8),
                  bg=BG, fg=MUTED, bd=0, cursor="hand2",
                  command=lambda: (
                      self.log_box.config(state="normal"),
                      self.log_box.delete("1.0", "end"),
                      self.log_box.config(state="disabled"))
                  ).pack(side="right", pady=(0, 2))

        lsb = tk.Scrollbar(log_wrap)
        lsb.pack(side="right", fill="y")
        self._log_height_px = 80          # current pixel height of the log box
        self._log_drag_y0   = 0           # y position when drag started
        self.log_box = tk.Text(
            log_wrap, bg="#0d0f18", fg=TEXT2, font=FT_MONO_SM,
            bd=0, highlightthickness=1, highlightbackground=BORDER,
            state="disabled", yscrollcommand=lsb.set, wrap="word")
        self.log_box.pack(fill="x")
        self.log_box.config(height=1)     # let pixel height drive sizing
        # Force pixel height immediately after layout
        self.after(50, lambda: self.log_box.config(
            height=max(1, self._log_height_px //
                       self.log_box.tk.call("font", "metrics",
                                            FT_MONO_SM, "-linespace"))))
        lsb.config(command=self.log_box.yview)
        for tag, col in [("ok", SUCCESS), ("err", ERROR), ("info", ACCENT2),
                         ("muted", MUTED), ("warn", WARN), ("text", TEXT2)]:
            self.log_box.tag_configure(tag, foreground=col)

    # ══════════════════════════════════════════════════════════════════════
    # LOG RESIZE  — drag handle above the log box
    # ══════════════════════════════════════════════════════════════════════
    def _log_drag_start(self, event):
        """Record Y position where the drag began."""
        self._log_drag_y0 = event.y_root
        # Snapshot current line height in pixels
        try:
            self._log_line_h = max(1, self.log_box.tk.call(
                "font", "metrics", FT_MONO_SM, "-linespace"))
        except Exception:
            self._log_line_h = 14

    def _log_drag_motion(self, event):
        """Resize the log box as the user drags the handle."""
        delta = self._log_drag_y0 - event.y_root   # drag UP → positive → bigger
        self._log_drag_y0 = event.y_root
        new_px = max(40, min(600, self._log_height_px + delta))
        self._log_height_px = new_px
        new_lines = max(2, new_px // self._log_line_h)
        self.log_box.config(height=new_lines)

    # ══════════════════════════════════════════════════════════════════════
    # SUDO TOGGLE
    # ══════════════════════════════════════════════════════════════════════
    def _toggle_sudo(self):
        self._sudo_mode = not self._sudo_mode
        if self._sudo_mode:
            self._sudo_btn.config(
                text="⚡ SUDO ON",
                bg="#1a0d2e", fg=SUDO_ON,
                activebackground="#260f45", activeforeground=SUDO_ON)
            # Show sudo password field inline in nav row 1
            self._sudo_pwd_row.pack(side="left", after=self._sudo_btn)
            self._log("[Sudo] Root mode enabled — connect to authenticate", SUDO_ON)
        else:
            self._sudo_btn.config(
                text="⚡ SUDO",
                bg="#0c0e15", fg=MUTED,
                activebackground="#1a0d2e", activeforeground=SUDO_ON)
            self._sudo_pwd_row.pack_forget()
            if self.root_ssh:
                try: self.root_ssh.close()
                except Exception: pass
                self.root_ssh = None
                self.root_cwd = "/"
                self.remote_list.delete(0, "end")
                self.remote_path_var.set("/")
            self._log("[Sudo] Root mode disabled", MUTED)
        self._refresh_browser_labels()

    def _refresh_browser_labels(self):
        """Update browser title and remote header to reflect current mode."""
        if self._sudo_mode:
            self._browser_title.config(text="\u229e  Browser  [ROOT]", fg=SUDO_ON)
            self._remote_hdr_lbl.config(text="  REMOTE (root)", fg=SUDO_ON)
        else:
            self._browser_title.config(text="\u229e  SFTP Browser", fg=TEXT)
            self._remote_hdr_lbl.config(text="  REMOTE", fg=ACCENT)

    # ══════════════════════════════════════════════════════════════════════
    # PAGE NAVIGATION
    # ══════════════════════════════════════════════════════════════════════
    def _show_page(self, key): pass  # no-op — single page layout

    def _connect_active(self):
        if self._connecting:
            self._log("[Connection] Already connecting, please wait...", WARN)
            return
        self._connecting = True
        if self._sudo_mode: self._root_connect()
        else:               self._sftp_connect()

    def _disconnect_active(self):
        if self._sudo_mode: self._root_disconnect()
        else:               self._sftp_disconnect()

    def _toggle_conn(self): pass  # no-op — inline nav bar has no collapse

    def _auto_collapse_conn(self): pass  # no-op

    def _set_conn_status(self, text, connected, color=None):
        """Update the nav bar connection dot and status label."""
        if not hasattr(self, "_conn_dot"):
            return
        col = color or (SUCCESS if connected else MUTED)
        self._conn_dot.config(fg=col)
        self.conn_status_bar.config(text=text, fg=col)

    def _toggle_auth(self):
        if self.auth_mode.get() == "password":
            self._key_entry_frame.place_forget()
            self._pass_entry.place(relx=0, rely=0.5, anchor="w",
                                   relwidth=1.0, height=24)
        else:
            self._pass_entry.place_forget()
            self._key_entry_frame.place(relx=0, rely=0.5, anchor="w",
                                        relwidth=1.0, height=26)

    def _browse_key(self):
        path = filedialog.askopenfilename(
            title="Select private key",
            filetypes=[("SSH Keys", "*.pem *.key id_rsa id_ed25519 *.ppk"),
                       ("All files", "*.*")])
        if path:
            self.key_var.set(path)

    # ══════════════════════════════════════════════════════════════════════
    # PING
    # ══════════════════════════════════════════════════════════════════════
    def _ping(self):
        ip   = self.ip_var.get().strip()
        port = self.port_var.get().strip()
        if not ip:
            messagebox.showwarning("Missing field", "Enter an IP / hostname.")
            return
        self.ping_lbl.config(text="  Testing...", fg=MUTED)
        self._log(f"[Ping] Testing {ip}:{port}...", ACCENT2)

        def _do():
            ok  = _ping_port(ip, port)
            col = SUCCESS if ok else ERROR
            txt = f"Port {port}: {'reachable' if ok else 'unreachable'}"
            self.after(0, lambda: self.ping_lbl.config(text=txt, fg=col))
            self.after(0, lambda: self._conn_dot.config(
                fg=col if ok else MUTED))
            self._log(f"[Ping] {txt}", col)

        threading.Thread(target=_do, daemon=True).start()

    # ══════════════════════════════════════════════════════════════════════
    # UI HELPERS
    # ══════════════════════════════════════════════════════════════════════
    def _entry(self, parent, label, width=20, side="top", default="", show=None):
        frm = tk.Frame(parent, bg=PANEL)
        frm.pack(side=side, anchor="w")
        tk.Label(frm, text=label, font=FT_LABEL,
                 bg=PANEL, fg=MUTED).pack(anchor="w")
        var = tk.StringVar(value=default)
        kw = dict(textvariable=var, width=width, font=FT_MONO,
                  bg="#0d0f18", fg=TEXT, insertbackground=ACCENT,
                  bd=0, highlightthickness=1,
                  highlightbackground=BORDER, highlightcolor=ACCENT,
                  relief="flat")
        if show:
            kw["show"] = show
        tk.Entry(frm, **kw).pack(pady=(2, 0), ipady=6)
        return var

    def _log(self, msg, color=None):
        if not hasattr(self, "log_box"):
            return
        tag = {SUCCESS: "ok", ERROR: "err", ACCENT2: "info",
               MUTED: "muted", WARN: "warn",
               SUDO_ON: "warn"}.get(color, "text")
        self.log_box.config(state="normal")
        self.log_box.insert("end", msg + "\n", tag)
        self.log_box.see("end")
        self.log_box.config(state="disabled")

    def _get_conn(self, silent=False):
        # Strip placeholder values from nav entries
        PLACEHOLDERS = {"IP / Hostname", "Username", "Password", "22"}
        ip   = self.ip_var.get().strip()
        port = self.port_var.get().strip()
        user = self.user_var.get().strip()
        pwd  = self.pass_var.get()
        key  = self.key_var.get().strip()
        if ip   in PLACEHOLDERS: ip   = ""
        if port in PLACEHOLDERS: port = "22"
        if user in PLACEHOLDERS: user = ""
        if pwd  in PLACEHOLDERS: pwd  = ""
        if not ip or not user:
            if not silent:
                self._log("[Connection] IP and username are required.", WARN)
            return None
        try:
            port = int(port) if port else 22
        except ValueError:
            self._log("[Connection] Invalid port.", ERROR)
            return None
        return ip, port, user, pwd, key

    def _ask_string(self, title, prompt, prefill=""):
        dlg = tk.Toplevel(self)
        dlg.title(title)
        dlg.configure(bg=BG)
        dlg.resizable(False, False)
        dlg.grab_set()
        tk.Label(dlg, text=prompt, font=FT,
                 bg=BG, fg=TEXT).pack(padx=20, pady=(14, 4))
        var = tk.StringVar(value=prefill)
        e = tk.Entry(dlg, textvariable=var, font=FT_MONO, width=34,
                     bg="#0d0f18", fg=TEXT, insertbackground=ACCENT,
                     bd=0, highlightthickness=1,
                     highlightbackground=BORDER, highlightcolor=ACCENT)
        e.pack(padx=20, pady=4, ipady=5)
        e.focus_set()
        e.select_range(0, "end")
        result = [None]

        def _ok():
            result[0] = var.get().strip()
            dlg.destroy()

        br = tk.Frame(dlg, bg=BG)
        br.pack(pady=12)
        tk.Button(br, text="OK", font=FT_BTN, bg=ACCENT, fg="#0a1510",
                  bd=0, padx=14, pady=5, cursor="hand2",
                  command=_ok).pack(side="left", padx=4)
        tk.Button(br, text="Cancel", font=FT_BTN, bg=PANEL2, fg=TEXT2,
                  bd=0, padx=14, pady=5, cursor="hand2",
                  command=dlg.destroy).pack(side="left", padx=4)
        e.bind("<Return>", lambda _: _ok())
        dlg.wait_window()
        return result[0]

    # ══════════════════════════════════════════════════════════════════════
    # REMOTE CWD PROPERTY  (dispatches to sftp_cwd or root_cwd)
    # ══════════════════════════════════════════════════════════════════════
    @property
    def _remote_cwd(self):
        return self.root_cwd if self._sudo_mode else self.sftp_cwd

    @_remote_cwd.setter
    def _remote_cwd(self, v):
        if self._sudo_mode: self.root_cwd = v
        else:               self.sftp_cwd = v

    @property
    def _local_cwd(self):
        return self.root_local_cwd if self._sudo_mode else self.sftp_local_cwd

    @_local_cwd.setter
    def _local_cwd(self, v):
        if self._sudo_mode: self.root_local_cwd = v
        else:               self.sftp_local_cwd = v

    # ══════════════════════════════════════════════════════════════════════
    # BROWSER  — resizable panes, editable paths, minimize/maximize
    # ══════════════════════════════════════════════════════════════════════
    def _build_browser(self, p):
        # ── Toolbar ───────────────────────────────────────────────────────
        tb = tk.Frame(p, bg=BG)
        tb.pack(fill="x", pady=(4, 4))
        self._browser_title = tk.Label(tb, text="\u229e  SFTP Browser",
                                       font=FT_BOLD, bg=BG, fg=TEXT)
        self._browser_title.pack(side="left")
        self._browser_status = tk.Label(tb, text="  Not connected",
                                        font=FT_LABEL, bg=BG, fg=MUTED)
        self._browser_status.pack(side="left", padx=(12, 0))

        # Show/hide dot-files toggle
        self._hidden_btn = tk.Button(
            tb, text="· Hidden: OFF", font=FT_SMALL,
            bg=PANEL2, fg=MUTED, activebackground=PANEL,
            activeforeground=TEXT, bd=0, cursor="hand2", padx=8, pady=4,
            command=self._toggle_hidden_files)
        self._hidden_btn.pack(side="right", padx=(0, 6))

        # Transfer progress bar (hidden until a transfer is running)
        self._prog_frame = tk.Frame(p, bg=BG)
        self._prog_lbl   = tk.Label(self._prog_frame, text="", font=FT_SMALL,
                                    bg=BG, fg=MUTED)
        self._prog_lbl.pack(anchor="w", padx=4)
        self._prog_canvas = tk.Canvas(self._prog_frame, bg=PANEL2, height=6,
                                      highlightthickness=1,
                                      highlightbackground=BORDER)
        self._prog_canvas.pack(fill="x", padx=4, pady=(2, 0))
        self._prog_bar_id = None
        # NOT packed yet — shown during transfers

        # ── PanedWindow — user drags the sash to resize ───────────────────
        self._paned = tk.PanedWindow(p, orient="horizontal",
                                     bg=BORDER, sashwidth=6,
                                     sashrelief="flat", bd=0,
                                     handlesize=0)
        self._paned.pack(fill="both", expand=True)

        # ── LOCAL pane ────────────────────────────────────────────────────
        lf = tk.Frame(self._paned, bg=PANEL,
                      highlightbackground=BORDER, highlightthickness=1)
        self._paned.add(lf, stretch="always", minsize=80)
        self._local_pane_frame = lf

        lhdr = tk.Frame(lf, bg=PANEL2)
        lhdr.pack(fill="x")
        tk.Label(lhdr, text="  LOCAL", font=FT_BOLD,
                 bg=PANEL2, fg=ACCENT2).pack(side="left", pady=6)
        self._local_min_btn = tk.Button(
            lhdr, text="⊟ Hide", font=FT_SMALL,
            bg="#1a1f35", fg="#6a7fc8",
            activebackground="#252a42", activeforeground=TEXT2,
            bd=0, cursor="hand2", padx=8, pady=3,
            command=self._toggle_local_pane)
        self._local_min_btn.pack(side="right", padx=4, pady=4)
        tk.Button(lhdr, text="↑ Parent", font=FT_SMALL,
                  bg="#1a2a1a", fg="#4dbb6a",
                  activebackground="#1f3320", activeforeground=ACCENT,
                  bd=0, cursor="hand2", padx=8, pady=3,
                  command=self._local_up).pack(side="right", padx=2, pady=4)
        tk.Button(lhdr, text="↺ Reload", font=FT_SMALL,
                  bg="#1a2535", fg="#4d90d6",
                  activebackground="#1f2f45", activeforeground=ACCENT2,
                  bd=0, cursor="hand2", padx=8, pady=3,
                  command=self._local_refresh).pack(side="right", padx=2, pady=4)

        # Editable path entry — press Enter to navigate
        self.local_path_var = tk.StringVar(value=self.sftp_local_cwd)
        local_path_entry = tk.Entry(
            lf, textvariable=self.local_path_var,
            font=FT_MONO_SM, bg="#0d0f18", fg=TEXT2,
            insertbackground=ACCENT, bd=0,
            highlightthickness=1, highlightbackground=BORDER,
            highlightcolor=ACCENT, relief="flat")
        local_path_entry.pack(fill="x", padx=6, pady=(4, 2), ipady=3)
        local_path_entry.bind("<Return>",
            lambda e: self._local_navigate(self.local_path_var.get().strip()))
        local_path_entry.bind("<FocusOut>",
            lambda e: self.local_path_var.set(self._local_cwd))

        self._local_body = tk.Frame(lf, bg=PANEL)
        self._local_body.pack(fill="both", expand=True)
        lsb  = tk.Scrollbar(self._local_body)
        lhsb = tk.Scrollbar(self._local_body, orient="horizontal")
        lsb.pack(side="right", fill="y")
        lhsb.pack(side="bottom", fill="x")
        self.local_list = tk.Listbox(
            self._local_body, bg="#0d0f18", fg=TEXT, font=FT_MONO,
            selectbackground=SEL_BG, selectforeground=TEXT,
            bd=0, highlightthickness=0,
            yscrollcommand=lsb.set, xscrollcommand=lhsb.set,
            selectmode="extended")
        self.local_list.pack(fill="both", expand=True, padx=(4, 0), pady=(0, 0))
        lsb.config(command=self.local_list.yview)
        lhsb.config(command=self.local_list.xview)
        self.local_list.bind("<Double-Button-1>", self._local_dblclick)
        self.local_list.bind("<Button-3>",        self._local_context_menu)
        # hand2 cursor on hover to hint double-click opens files
        self.local_list.bind("<Motion>", lambda e: self.local_list.config(cursor="hand2"))

        la = tk.Frame(lf, bg=PANEL)
        la.pack(fill="x", padx=4, pady=4)
        tk.Button(la, text="Upload \u2192", font=FT_BTN,
                  bg=ACCENT, fg="#0a1510", activebackground="#27b589",
                  activeforeground="#0a1510", bd=0, padx=10, pady=5,
                  cursor="hand2", command=self._browser_upload).pack(side="left")
        tk.Button(la, text="+ Folder", font=FT_SMALL,
                  bg=PANEL2, fg=TEXT2, activebackground=PANEL,
                  activeforeground=TEXT, bd=0, padx=8, pady=5,
                  cursor="hand2", command=self._local_mkdir).pack(side="left", padx=(6, 0))

        # ── REMOTE pane ───────────────────────────────────────────────────
        rf = tk.Frame(self._paned, bg=PANEL,
                      highlightbackground=BORDER, highlightthickness=1)
        self._paned.add(rf, stretch="always", minsize=80)
        self._remote_pane_frame = rf

        rhdr = tk.Frame(rf, bg=PANEL2)
        rhdr.pack(fill="x")
        self._remote_hdr_lbl = tk.Label(rhdr, text="  REMOTE",
                                        font=FT_BOLD, bg=PANEL2, fg=ACCENT)
        self._remote_hdr_lbl.pack(side="left", pady=6)
        self._remote_min_btn = tk.Button(
            rhdr, text="⊟ Hide", font=FT_SMALL,
            bg="#1a1f35", fg="#6a7fc8",
            activebackground="#252a42", activeforeground=TEXT2,
            bd=0, cursor="hand2", padx=8, pady=3,
            command=self._toggle_remote_pane)
        self._remote_min_btn.pack(side="right", padx=4, pady=4)
        tk.Button(rhdr, text="↑ Parent", font=FT_SMALL,
                  bg="#1a2a1a", fg="#4dbb6a",
                  activebackground="#1f3320", activeforeground=ACCENT,
                  bd=0, cursor="hand2", padx=8, pady=3,
                  command=self._remote_up).pack(side="right", padx=2, pady=4)
        tk.Button(rhdr, text="↺ Reload", font=FT_SMALL,
                  bg="#1a2535", fg="#4d90d6",
                  activebackground="#1f2f45", activeforeground=ACCENT2,
                  bd=0, cursor="hand2", padx=8, pady=3,
                  command=self._remote_refresh).pack(side="right", padx=2, pady=4)

        # Editable remote path entry
        self.remote_path_var = tk.StringVar(value="/")
        remote_path_entry = tk.Entry(
            rf, textvariable=self.remote_path_var,
            font=FT_MONO_SM, bg="#0d0f18", fg=TEXT2,
            insertbackground=ACCENT, bd=0,
            highlightthickness=1, highlightbackground=BORDER,
            highlightcolor=ACCENT, relief="flat")
        remote_path_entry.pack(fill="x", padx=6, pady=(4, 2), ipady=3)
        remote_path_entry.bind("<Return>",
            lambda e: self._remote_navigate(self.remote_path_var.get().strip()))
        remote_path_entry.bind("<FocusOut>",
            lambda e: self.remote_path_var.set(self._remote_cwd))

        self._remote_body = tk.Frame(rf, bg=PANEL)
        self._remote_body.pack(fill="both", expand=True)
        rsb  = tk.Scrollbar(self._remote_body)
        rhsb = tk.Scrollbar(self._remote_body, orient="horizontal")
        rsb.pack(side="right", fill="y")
        rhsb.pack(side="bottom", fill="x")
        self.remote_list = tk.Listbox(
            self._remote_body, bg="#0d0f18", fg=TEXT, font=FT_MONO,
            selectbackground=SEL_BG, selectforeground=TEXT,
            bd=0, highlightthickness=0,
            yscrollcommand=rsb.set, xscrollcommand=rhsb.set,
            selectmode="extended")   # multi-select
        self.remote_list.pack(fill="both", expand=True, padx=(4, 0), pady=(0, 0))
        rsb.config(command=self.remote_list.yview)
        rhsb.config(command=self.remote_list.xview)
        self.remote_list.bind("<Double-Button-1>", self._remote_dblclick)
        self.remote_list.bind("<Button-3>",        self._remote_context_menu)
        self.remote_list.bind("<Motion>", lambda e: self.remote_list.config(cursor="hand2"))

        ra = tk.Frame(rf, bg=PANEL)
        ra.pack(fill="x", padx=4, pady=4)
        tk.Button(ra, text="\u2190 Download", font=FT_BTN,
                  bg=ACCENT2, fg=TEXT, activebackground="#2d6fcc",
                  activeforeground=TEXT, bd=0, padx=10, pady=5,
                  cursor="hand2", command=self._browser_download).pack(side="left")
        tk.Button(ra, text="\u21d4 Diff", font=FT_SMALL,
                  bg=PANEL2, fg=ACCENT2, activebackground=PANEL,
                  activeforeground=ACCENT2, bd=0, padx=8, pady=5,
                  cursor="hand2", command=self._sftp_diff).pack(side="left", padx=(6, 0))
        tk.Button(ra, text="\u2715 Delete", font=FT_SMALL,
                  bg=PANEL2, fg=ERROR, activebackground=PANEL,
                  activeforeground=ERROR, bd=0, padx=8, pady=5,
                  cursor="hand2", command=self._remote_delete).pack(side="left", padx=(6, 0))
        tk.Button(ra, text="+ Folder", font=FT_SMALL,
                  bg=PANEL2, fg=TEXT2, activebackground=PANEL,
                  activeforeground=TEXT, bd=0, padx=8, pady=5,
                  cursor="hand2", command=self._remote_mkdir).pack(side="left", padx=(6, 0))

        # Minimize state tracking
        self._local_minimized       = False
        self._remote_minimized      = False
        self._local_pane_last_size  = 0
        self._remote_pane_last_size = 0

        self._local_refresh()

    # ── Browser dispatch (routes to SFTP or Root based on mode) ──────────
    def _browser_upload(self):
        if self._sudo_mode: self._root_upload()
        else:               self._sftp_upload()

    def _browser_download(self):
        if self._sudo_mode: self._root_download()
        else:               self._sftp_download()

    def _remote_refresh(self):
        if self._sudo_mode: self._root_remote_refresh()
        else:               self._sftp_remote_refresh()

    def _remote_up(self):
        if self._sudo_mode: self._root_remote_up()
        else:               self._sftp_remote_up()

    def _remote_dblclick(self, e):
        if self._sudo_mode: self._root_remote_dblclick(e)
        else:               self._sftp_remote_dblclick(e)

    def _remote_context_menu(self, e):
        if self._sudo_mode: self._root_context_menu(e)
        else:               self._sftp_context_menu(e)

    def _remote_delete(self):
        if self._sudo_mode: self._root_delete()
        else:               self._sftp_delete()

    def _remote_mkdir(self):
        if self._sudo_mode: self._root_mkdir()
        else:               self._sftp_mkdir()

    def _populate_remote(self, items):
        self.remote_list.delete(0, "end")
        for item in items:
            text, color = item if isinstance(item, tuple) else (item, TEXT)
            self.remote_list.insert("end", text)
            if color == ERROR:
                self.remote_list.itemconfig(self.remote_list.size() - 1, fg=ERROR)


    def _local_navigate(self, path):
        """Navigate local pane to typed path."""
        path = os.path.expanduser(path)
        if os.path.isdir(path):
            self._local_cwd = path
            self._local_refresh()
        else:
            self._log(f"[Local] Not a directory: {path}", ERROR)
            self.local_path_var.set(self._local_cwd)

    def _remote_navigate(self, path):
        """Navigate remote pane to typed path."""
        if not (self.sftp_conn or self.root_ssh):
            self.remote_path_var.set(self._remote_cwd)
            return
        self._remote_cwd = path
        self._remote_refresh()

    def _toggle_local_pane(self):
        """Minimize or restore the local pane."""
        total = self._paned.winfo_width()
        sash  = self._paned.sash_coord(0)[0]
        if not self._local_minimized:
            # Save current position and collapse to header-only width
            self._local_pane_last_size = sash
            self._paned.sash_place(0, 6, 1)
            self._local_body.pack_forget()
            self._local_min_btn.config(text="▶ Show")   # ▶ restore arrow
            self._local_minimized = True
        else:
            restore = self._local_pane_last_size or total // 2
            self._paned.sash_place(0, restore, 1)
            self._local_body.pack(fill="both", expand=True)
            self._local_min_btn.config(text="⊟ Hide")   # — minimize dash
            self._local_minimized = False

    def _toggle_remote_pane(self):
        """Minimize or restore the remote pane."""
        total = self._paned.winfo_width()
        sash  = self._paned.sash_coord(0)[0]
        if not self._remote_minimized:
            self._remote_pane_last_size = total - sash
            self._paned.sash_place(0, total - 6, 1)
            self._remote_body.pack_forget()
            self._remote_min_btn.config(text="▶ Show")  # ◀ restore arrow
            self._remote_minimized = True
        else:
            restore = self._remote_pane_last_size or total // 2
            self._paned.sash_place(0, total - restore, 1)
            self._remote_body.pack(fill="both", expand=True)
            self._remote_min_btn.config(text="⊟ Hide")
            self._remote_minimized = False

    # ══════════════════════════════════════════════════════════════════════
    # SFTP – connection
    # ══════════════════════════════════════════════════════════════════════
    # ══════════════════════════════════════════════════════════════════════
    # HEARTBEAT — detect dropped connections every 20s
    # ══════════════════════════════════════════════════════════════════════
    def _start_heartbeat(self):
        """Start periodic connection check."""
        if self._heartbeat_id:
            self.after_cancel(self._heartbeat_id)
        self._heartbeat_id = self.after(20000, self._heartbeat_tick)

    def _heartbeat_tick(self):
        """Send a keepalive; disconnect gracefully if connection dropped."""
        sess = self.root_session if self._sudo_mode else self.session
        if not sess:
            return
        try:
            sess.ssh.get_transport().send_ignore()
            self._heartbeat_id = self.after(20000, self._heartbeat_tick)
        except Exception:
            self._log("[Connection] Lost — disconnecting", WARN)
            if self._sudo_mode:
                self.after(0, self._root_disconnect)
            else:
                self.after(0, self._sftp_disconnect)

    # ══════════════════════════════════════════════════════════════════════
    # SHOW / HIDE HIDDEN FILES
    # ══════════════════════════════════════════════════════════════════════
    def _toggle_hidden_files(self):
        self._show_hidden = not self._show_hidden
        if self._show_hidden:
            self._hidden_btn.config(text="· Hidden: ON",  fg=ACCENT2,
                                    bg=PANEL)
        else:
            self._hidden_btn.config(text="· Hidden: OFF", fg=MUTED,
                                    bg=PANEL2)
        self._local_refresh()
        self._remote_refresh()

    def _should_show(self, name):
        """Return True if the entry should be visible given current hidden setting."""
        if self._show_hidden:
            return True
        return not name.startswith(".")

    # ══════════════════════════════════════════════════════════════════════
    # TRANSFER PROGRESS BAR
    # ══════════════════════════════════════════════════════════════════════
    def _prog_show(self, label=""):
        """Show progress bar below browser toolbar."""
        if not hasattr(self, "_prog_frame"):
            return
        self._prog_frame.pack(fill="x", padx=4, pady=(0, 4))
        self._prog_lbl.config(text=label)
        if self._prog_bar_id:
            self._prog_canvas.delete(self._prog_bar_id)
            self._prog_bar_id = None

    def _prog_update(self, cur, total, label=""):
        """Update progress bar fill."""
        if not hasattr(self, "_prog_canvas"):
            return
        self._prog_lbl.config(text=label)
        self._prog_canvas.update_idletasks()
        w = self._prog_canvas.winfo_width()
        if w < 2:
            return
        x1 = int(w * cur / max(total, 1))
        if self._prog_bar_id:
            self._prog_canvas.coords(self._prog_bar_id, 0, 0, x1, 6)
        else:
            self._prog_bar_id = self._prog_canvas.create_rectangle(
                0, 0, x1, 6, fill=ACCENT, outline="")

    def _prog_hide(self):
        """Hide progress bar after transfer completes."""
        if hasattr(self, "_prog_frame"):
            self._prog_frame.pack_forget()
        if self._prog_bar_id and hasattr(self, "_prog_canvas"):
            self._prog_canvas.delete(self._prog_bar_id)
            self._prog_bar_id = None

    def _sftp_connect_silent(self):
        if not PARAMIKO_OK:
            return
        if not self._get_conn(silent=True):
            return
        self._sftp_connect()

    def _sftp_connect(self):
        if not PARAMIKO_OK:
            self._log("[SSH] pip install paramiko", ERROR)
            return
        conn = self._get_conn()
        if not conn:
            return
        ip, port, user, pwd, key = conn

        def _do():
            try:
                self._log(f"\n[SSH] Connecting to {user}@{ip}:{port}...", ACCENT2)
                ssh     = _ssh_connect(ip, port, user, pwd, key)
                session = ShellSession(ssh, log_fn=self._log)

                # Store session and compat aliases
                self.session       = session
                self.session_ssh   = ssh
                self.session_cwd   = session.cwd
                self.sftp_ssh      = ssh
                self.sftp_conn     = session   # compat alias
                self.sftp_cwd      = session.cwd

                self._connecting = False
                mode = "SFTP" if session.has_sftp else "shell-only"
                cksum = session.checksum or "none"
                self._log(
                    f"[SSH] Connected — mode: {mode}, checksum: {cksum}",
                    SUCCESS)
                self.after(0, lambda: self._browser_status.config(
                    text=f"  Connected ({mode}): {user}@{ip}", fg=SUCCESS))
                self.after(0, lambda: self._set_conn_status(
                    f"{user}@{ip}", True))
                self.after(50, self._start_heartbeat)
                self.after(0, self._sftp_remote_refresh)
            except Exception as e:
                self._connecting = False
                self._log(f"[SSH] ERROR: {e}", ERROR)

        threading.Thread(target=_do, daemon=True).start()

    def _sftp_disconnect(self):
        if self.session:
            try: self.session.close()
            except Exception: pass
        self.session = self.session_ssh = None
        self.sftp_conn = self.sftp_ssh = None
        self.sftp_cwd = self.session_cwd = "/"
        self.remote_list.delete(0, "end")
        self.remote_path_var.set("/")
        self._browser_status.config(text="  Not connected", fg=MUTED)
        self._set_conn_status("not connected", False)
        self._log("[SSH] Disconnected", MUTED)

    # ══════════════════════════════════════════════════════════════════════
    # SESSION – remote browser (SFTP or shell-only via ShellSession)
    # ══════════════════════════════════════════════════════════════════════
    def _sftp_remote_refresh(self):
        if not self.session:
            return
        self.sftp_cwd = self.session_cwd
        self.remote_path_var.set(self.sftp_cwd)

        def _do():
            try:
                entries = self.session.listdir(self.sftp_cwd)
                items = []
                for name, is_dir, readable in entries:
                    if not self._should_show(name):
                        continue
                    col = ERROR if not readable else                           (MUTED if name.startswith(".") else TEXT)
                    label = f"📁 {name}" if is_dir else f"     {name}"
                    items.append((label, col))
                self.after(0, lambda: self._populate_remote(items))
            except Exception as e:
                err_str = str(e).lower()
                if "permission" in err_str or "denied" in err_str:
                    self._log(f"[Session] Permission denied: {self.sftp_cwd}",
                              ERROR)
                    self._log(
                        "[Session]   → Enable SUDO mode to browse protected folders",
                        WARN)
                    self.after(0, lambda: self._populate_remote(
                        [("[!] Permission denied — enable SUDO mode", ERROR)]))
                else:
                    self._log(f"[Session] List error: {e}", ERROR)

        threading.Thread(target=_do, daemon=True).start()

    def _sftp_remote_up(self):
        if not self.session:
            return
        parent = self.session_cwd.rstrip("/")
        parent = parent[:parent.rfind("/")] or "/"
        self.session_cwd = self.sftp_cwd = parent
        self._sftp_remote_refresh()

    def _sftp_remote_dblclick(self, _e):
        if not self.session:
            return
        sel = self.remote_list.curselection()
        if not sel:
            return
        raw = self.remote_list.get(sel[0])
        if raw.startswith("📁"):
            name = raw.strip()[2:].strip()
            self.session_cwd = self.sftp_cwd =                 self.session_cwd.rstrip("/") + "/" + name
            self._sftp_remote_refresh()
        else:
            self._sftp_open_editor(raw.strip())

    def _sftp_context_menu(self, event):
        if not self.session:
            return
        idx = self.remote_list.nearest(event.y)
        if idx < 0:
            return
        self.remote_list.selection_clear(0, "end")
        self.remote_list.selection_set(idx)
        self.remote_list.activate(idx)
        raw    = self.remote_list.get(idx)
        is_dir = raw.startswith("📁")
        menu = tk.Menu(self, tearoff=0, bg=PANEL2, fg=TEXT,
                       activebackground=SEL_BG, activeforeground=TEXT,
                       font=FT_SMALL, bd=0, relief="flat")
        menu.add_command(label="✏  Rename",  command=self._sftp_rename)
        menu.add_separator()
        menu.add_command(label="✕  Delete",
                         foreground=ERROR, activeforeground=ERROR,
                         command=self._sftp_delete)
        if not is_dir:
            menu.add_separator()
            menu.add_command(label="✎  Open / Edit",
                             command=lambda: self._sftp_open_editor(
                                 raw.strip().removeprefix("📁").strip()))
        try:     menu.tk_popup(event.x_root, event.y_root)
        finally: menu.grab_release()

    def _sftp_rename(self):
        if not self.session:
            return
        sel = self.remote_list.curselection()
        if not sel:
            return
        raw  = self.remote_list.get(sel[0])
        name = raw.strip().removeprefix("📁").strip()
        old  = self.session_cwd.rstrip("/") + "/" + name
        new_name = self._ask_string("Rename", "New name:", prefill=name)
        if not new_name or new_name == name:
            return
        new = self.session_cwd.rstrip("/") + "/" + new_name

        def _do():
            try:
                self.session.rename(old, new)
                self._log(f"[Session] Renamed: {name} → {new_name}", SUCCESS)
                self.after(0, self._sftp_remote_refresh)
            except Exception as e:
                self._log(f"[Session] Rename error: {e}", ERROR)

        threading.Thread(target=_do, daemon=True).start()

    def _sftp_delete(self):
        if not self.session:
            return
        sel = self.remote_list.curselection()
        if not sel:
            return
        raw    = self.remote_list.get(sel[0])
        name   = raw.strip().removeprefix("📁").strip()
        is_dir = raw.startswith("📁")
        path   = self.session_cwd.rstrip("/") + "/" + name
        if not messagebox.askyesno(
                "Confirm",
                f"Delete {'folder' if is_dir else 'file'}:\n{path}?"):
            return

        def _do():
            try:
                self.session.remove(path, is_dir=is_dir)
                self._log(f"[Session] Deleted: {name}", SUCCESS)
                self.after(0, self._sftp_remote_refresh)
            except Exception as e:
                self._log(f"[Session] Delete error: {e}", ERROR)

        threading.Thread(target=_do, daemon=True).start()

    def _sftp_mkdir(self):
        if not self.session:
            return
        name = self._ask_string("New remote folder", "Folder name:")
        if not name:
            return
        path = self.session_cwd.rstrip("/") + "/" + name

        def _do():
            try:
                self.session.makedirs(path)
                self._log(f"[Session] Folder created: {path}", SUCCESS)
                self.after(0, self._sftp_remote_refresh)
            except Exception as e:
                self._log(f"[Session] mkdir error: {e}", ERROR)

        threading.Thread(target=_do, daemon=True).start()

    def _sftp_upload(self):
        if not self.session:
            messagebox.showwarning("Not connected", "Connect first.")
            return
        sels = self.local_list.curselection()
        if not sels:
            messagebox.showwarning("Nothing selected", "Select a file.")
            return
        pairs = []
        for idx in sels:
            raw  = self.local_list.get(idx)
            name = raw.strip().removeprefix("📁").strip()
            lp   = os.path.join(self._local_cwd, name)
            rp   = self.session_cwd.rstrip("/") + "/" + name
            if os.path.isdir(lp):
                continue
            if self.session.stat_exists(rp):
                if not messagebox.askyesno(
                        "Overwrite?",
                        f"{name} already exists on remote. Overwrite?"):
                    continue
            pairs.append((lp, rp, name))
        if not pairs:
            return

        def _do():
            total = len(pairs)
            self.after(0, lambda: self._prog_show(f"Uploading 0/{total}..."))
            for i, (lp, rp, name) in enumerate(pairs, 1):
                self._log(f"[Upload] {name}...", ACCENT2)
                def _cb(done, size, fi=i, t=total, n=name):
                    pct = done / max(size, 1)
                    self.after(0, lambda p=pct, fi=fi, t=t, n=n:
                        self._prog_update(p, 1.0,
                            f"Uploading {fi}/{t}: {n} ({int(p*100)}%)"))
                try:
                    ok = self.session.upload(lp, rp, progress_cb=_cb)
                    if ok:
                        self._log(f"[Upload] ✓ {name}", SUCCESS)
                    else:
                        self._log(f"[Upload] ✗ {name} — transfer failed", ERROR)
                except Exception as e:
                    self._log(f"[Upload] ✗ {name}: {e}", ERROR)
            self.after(0, self._prog_hide)
            self.after(0, self._sftp_remote_refresh)

        threading.Thread(target=_do, daemon=True).start()

    def _sftp_download(self):
        if not self.session:
            messagebox.showwarning("Not connected", "Connect first.")
            return
        sels = self.remote_list.curselection()
        if not sels:
            messagebox.showwarning("Nothing selected", "Select a file.")
            return
        pairs = []
        for idx in sels:
            raw = self.remote_list.get(idx)
            if raw.startswith("📁"):
                continue
            name = raw.strip()
            rp   = self.session_cwd.rstrip("/") + "/" + name
            lp   = os.path.join(self._local_cwd, name)
            if os.path.exists(lp):
                if not messagebox.askyesno(
                        "Overwrite?",
                        f"{name} already exists locally. Overwrite?"):
                    continue
            pairs.append((rp, lp, name))
        if not pairs:
            return

        def _do():
            total = len(pairs)
            self.after(0, lambda: self._prog_show(f"Downloading 0/{total}..."))
            for i, (rp, lp, name) in enumerate(pairs, 1):
                self._log(f"[Download] {name}...", ACCENT2)
                def _cb(done, size, fi=i, t=total, n=name):
                    pct = done / max(size, 1)
                    self.after(0, lambda p=pct, fi=fi, t=t, n=n:
                        self._prog_update(p, 1.0,
                            f"Downloading {fi}/{t}: {n} ({int(p*100)}%)"))
                try:
                    ok = self.session.download(rp, lp, progress_cb=_cb)
                    if ok:
                        self._log(f"[Download] ✓ {name}", SUCCESS)
                    else:
                        self._log(f"[Download] ✗ {name} — transfer failed", ERROR)
                except Exception as e:
                    self._log(f"[Download] ✗ {name}: {e}", ERROR)
            self.after(0, self._prog_hide)
            self.after(0, self._local_refresh)

        threading.Thread(target=_do, daemon=True).start()

    def _sftp_open_editor(self, name):
        remote_path = self.session_cwd.rstrip("/") + "/" + name
        try:
            content = self.session.read_text(remote_path)
        except Exception as e:
            self._log(f"[SFTP] Cannot open {name}: {e}", ERROR)
            return

        win = tk.Toplevel(self)
        win.title(f"Editor: {remote_path}")
        win.configure(bg=BG)
        win.geometry("800x560")

        bar = tk.Frame(win, bg=PANEL2)
        bar.pack(fill="x", padx=12, pady=(10, 4))
        tk.Label(bar, text=remote_path, font=FT_MONO_SM,
                 bg=PANEL2, fg=ACCENT2).pack(side="left")

        tf = tk.Frame(win, bg=BG)
        tf.pack(fill="both", expand=True, padx=12, pady=4)
        tsb = tk.Scrollbar(tf)
        tsb.pack(side="right", fill="y")
        editor = tk.Text(tf, bg="#0d0f18", fg=TEXT, font=FT_MONO,
                         insertbackground=ACCENT, bd=0,
                         highlightthickness=1, highlightbackground=BORDER,
                         yscrollcommand=tsb.set, wrap="none")
        editor.pack(fill="both", expand=True)
        tsb.config(command=editor.yview)
        editor.insert("1.0", content)

        def _save():
            try:
                self.session.write_text(remote_path, editor.get("1.0", "end-1c"))
                self._log(f"[Editor] ✓ Saved: {remote_path}", SUCCESS)
                win.destroy()
            except Exception as e:
                self._log(f"[Editor] Save error: {e}", ERROR)

        br = tk.Frame(win, bg=BG)
        br.pack(fill="x", padx=12, pady=(0, 10))
        tk.Button(br, text="  SAVE  ", font=FT_BTN,
                  bg=ACCENT, fg="#0a1510", bd=0, padx=16, pady=8,
                  cursor="hand2", command=_save).pack(side="left")
        tk.Button(br, text="Cancel", font=FT_SMALL,
                  bg=PANEL2, fg=TEXT2, bd=0, padx=10, pady=8,
                  cursor="hand2", command=win.destroy).pack(side="left", padx=(8, 0))

    def _sftp_diff(self):
        if self._sudo_mode:
            messagebox.showinfo("Info",
                "Diff is not available in SUDO mode.")
            return
        if not self.session:
            return
        lsel = self.local_list.curselection()
        rsel = self.remote_list.curselection()
        if not lsel or not rsel:
            messagebox.showinfo("Diff",
                "Select one LOCAL file and one REMOTE file.")
            return
        lraw = self.local_list.get(lsel[0]).strip()
        rraw = self.remote_list.get(rsel[0]).strip()
        if lraw.startswith("📁") or rraw.startswith("📁"):
            messagebox.showinfo("Diff", "Select files, not folders.")
            return
        lname = lraw.removeprefix("📁").strip()
        rname = rraw.removeprefix("📁").strip()
        lpath = os.path.join(self._local_cwd, lname)
        rpath = self.session_cwd.rstrip("/") + "/" + rname
        try:
            with open(lpath, "r", errors="replace") as f:
                lc = f.readlines()
            rc = self.session.read_text(rpath).splitlines(keepends=True)
        except Exception as e:
            self._log(f"[Diff] Read error: {e}", ERROR)
            return
        import difflib
        diff = list(difflib.unified_diff(
            lc, rc,
            fromfile=f"LOCAL: {lname}",
            tofile=f"REMOTE: {rname}"))

        win = tk.Toplevel(self)
        win.title(f"Diff: {lname} ↔ {rname}")
        win.configure(bg=BG)
        win.geometry("900x560")
        tk.Label(win, text=f"{lpath}   ↔   {rpath}",
                 font=FT_SMALL, bg=BG, fg=MUTED).pack(
            anchor="w", padx=14, pady=(10, 4))
        dsb = tk.Scrollbar(win)
        dsb.pack(side="right", fill="y")
        dtxt = tk.Text(win, bg="#0d0f18", fg=TEXT2, font=FT_MONO_SM,
                       bd=0, highlightthickness=0,
                       yscrollcommand=dsb.set, wrap="none")
        dtxt.pack(fill="both", expand=True, padx=8, pady=(0, 10))
        dsb.config(command=dtxt.yview)
        dtxt.tag_configure("add", foreground=SUCCESS)
        dtxt.tag_configure("rem", foreground=ERROR)
        dtxt.tag_configure("hdr", foreground=ACCENT2)
        if not diff:
            dtxt.insert("end", "  Files are identical ✓", "add")
        else:
            for line in diff:
                tag = ("add" if line.startswith("+") else
                       "rem" if line.startswith("-") else
                       "hdr" if line.startswith("@") else "")
                dtxt.insert("end", line, tag)
        dtxt.config(state="disabled")

    # ══════════════════════════════════════════════════════════════════════
    # ROOT/SUDO – connection
    # ══════════════════════════════════════════════════════════════════════
    def _root_connect(self):
        if not PARAMIKO_OK:
            self._log("[Root] pip install paramiko", ERROR)
            return
        conn = self._get_conn()
        if not conn:
            return
        ip, port, user, pwd, key = conn
        sudo_pwd = self.sudo_pwd_var.get().strip() or pwd

        def _do():
            try:
                self._log(f"\n[Root] Connecting {user}@{ip}:{port}...", ACCENT2)
                ssh  = _ssh_connect(ip, port, user, pwd, key)
                chan = ssh.get_transport().open_session()
                chan.exec_command("sudo -S -k true")
                chan.sendall((sudo_pwd + "\n").encode())
                rc = chan.recv_exit_status()
                if rc != 0:
                    self._connecting = False
                    self._log("[Root] sudo failed — wrong password?", ERROR)
                    ssh.close()
                    return
                self.root_ssh      = ssh
                self.root_sudo_pwd = sudo_pwd
                self.root_cwd      = "/"
                self._connecting = False
                self._log("[Root] Connected with sudo access", SUCCESS)
                self.after(0, lambda: self._browser_status.config(
                    text=f"  Connected (sudo): {user}@{ip}", fg=SUDO_ON))
                self.after(0, lambda: self._set_conn_status(f"sudo:{user}@{ip}", True, SUDO_ON))
                self.after(0, self._root_remote_refresh)
            except Exception as e:
                self._connecting = False
                self._log(f"[Root] ERROR: {e}", ERROR)

        threading.Thread(target=_do, daemon=True).start()

    def _root_disconnect(self):
        if self.root_ssh:
            try: self.root_ssh.close()
            except Exception: pass
        self.root_ssh = None
        self.root_sudo_pwd = None
        self.root_cwd = "/"
        self.remote_list.delete(0, "end")
        self.remote_path_var.set("/")
        self._browser_status.config(text="  Not connected", fg=MUTED)
        self._set_conn_status("not connected", False)
        self._log("[Root] Disconnected", MUTED)

    def _root_ssh_cmd(self, cmd):
        _, stdout, stderr = self.root_ssh.exec_command(
            f"sudo {cmd}", timeout=15)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace").strip()
        rc  = stdout.channel.recv_exit_status()
        return out, err, rc

    # ══════════════════════════════════════════════════════════════════════
    # ROOT/SUDO – remote browser
    # ══════════════════════════════════════════════════════════════════════
    def _root_remote_refresh(self):
        if not self.root_ssh:
            return
        self.remote_path_var.set(self.root_cwd)  # keep entry in sync
        self._log(f"[Root] Listing: {self.root_cwd}", MUTED)

        def _do():
            try:
                out, err, rc = self._root_ssh_cmd(
                    f'ls -1p --color=never {shlex.quote(self.root_cwd)} 2>&1')
                if rc != 0:
                    self._log(f"[Root] ls failed: {err.strip()}", ERROR)
                    self.after(0, lambda: self._populate_remote(
                        [("[!] Permission denied", ERROR)]))
                    return
                lines = [l for l in out.splitlines() if l.strip()]
                # Fetch permissions
                out2, _, _ = self._root_ssh_cmd(
                    f'ls -la --color=never {shlex.quote(self.root_cwd)} 2>/dev/null')
                perm_map = {}
                for line in out2.splitlines():
                    parts = line.split()
                    if len(parts) >= 9:
                        fname = parts[-1].rstrip("/")
                        perms = parts[0]
                        perm_map[fname] = (
                            "r" in perms[1:4] or
                            "r" in perms[4:7] or
                            "r" in perms[7:10])
                dirs  = sorted([l.rstrip("/") for l in lines if l.endswith("/")])
                files = sorted([l for l in lines if not l.endswith("/")])
                items = (
                    [(f"📁 {d}",
                      ERROR if not perm_map.get(d, True) else
                      (MUTED if d.startswith(".") else TEXT))
                     for d in dirs if self._should_show(d)] +
                    [(f"     {f}",
                      ERROR if not perm_map.get(f, True) else
                      (MUTED if f.startswith(".") else TEXT))
                     for f in files if self._should_show(f)]
                )
                self.after(0, lambda: self._populate_remote(items))
            except Exception as e:
                self._log(f"[Root] Refresh error: {e}", ERROR)

        threading.Thread(target=_do, daemon=True).start()

    def _root_remote_up(self):
        if not self.root_ssh:
            return
        parent = self.root_cwd.rstrip("/")
        parent = parent[:parent.rfind("/")] or "/"
        self.root_cwd = parent
        self._root_remote_refresh()

    def _root_remote_dblclick(self, _e):
        if not self.root_ssh:
            return
        sel = self.remote_list.curselection()
        if not sel:
            return
        raw = self.remote_list.get(sel[0])
        if not raw.startswith("📁"):
            return
        name = raw.strip().removeprefix("📁").strip()
        self.root_cwd = self.root_cwd.rstrip("/") + "/" + name
        self._root_remote_refresh()

    def _root_context_menu(self, event):
        if not self.root_ssh:
            return
        idx = self.remote_list.nearest(event.y)
        if idx < 0:
            return
        self.remote_list.selection_clear(0, "end")
        self.remote_list.selection_set(idx)
        self.remote_list.activate(idx)
        menu = tk.Menu(self, tearoff=0, bg=PANEL2, fg=TEXT,
                       activebackground=SEL_BG, activeforeground=TEXT,
                       font=FT_SMALL, bd=0, relief="flat")
        menu.add_command(label="✏  Rename", command=self._root_rename)
        menu.add_separator()
        menu.add_command(label="✕  Delete",
                         foreground=ERROR, activeforeground=ERROR,
                         command=self._root_delete)
        try:     menu.tk_popup(event.x_root, event.y_root)
        finally: menu.grab_release()

    def _root_rename(self):
        if not self.root_ssh:
            return
        sel = self.remote_list.curselection()
        if not sel:
            return
        raw  = self.remote_list.get(sel[0])
        name = raw.strip().removeprefix("📁").strip()
        old  = self.root_cwd.rstrip("/") + "/" + name
        new_name = self._ask_string("Rename (sudo)", "New name:", prefill=name)
        if not new_name or new_name == name:
            return
        new = self.root_cwd.rstrip("/") + "/" + new_name

        def _do():
            try:
                _, err, rc = self._root_ssh_cmd(f'mv {shlex.quote(old)} {shlex.quote(new)}')
                if rc != 0:
                    self._log(f"[Root] Rename error: {err}", ERROR)
                else:
                    self._log(f"[Root] Renamed: {name} → {new_name}", SUCCESS)
                    self.after(0, self._root_remote_refresh)
            except Exception as e:
                self._log(f"[Root] ERROR: {e}", ERROR)

        threading.Thread(target=_do, daemon=True).start()

    def _root_delete(self):
        if not self.root_ssh:
            return
        sel = self.remote_list.curselection()
        if not sel:
            return
        raw    = self.remote_list.get(sel[0])
        name   = raw.strip().removeprefix("📁").strip()
        is_dir = raw.startswith("📁")
        path   = self.root_cwd.rstrip("/") + "/" + name
        if not messagebox.askyesno(
                "Confirm",
                f"Delete (sudo) {'folder' if is_dir else 'file'}:\n{path}?"):
            return

        def _do():
            try:
                cmd = f'rm -rf {shlex.quote(path)}' if is_dir else f'rm -f {shlex.quote(path)}'
                _, err, rc = self._root_ssh_cmd(cmd)
                if rc != 0:
                    self._log(f"[Root] rm failed: {err}", ERROR)
                else:
                    self._log(f"[Root] Deleted: {name}", SUCCESS)
                    self.after(0, self._root_remote_refresh)
            except Exception as e:
                self._log(f"[Root] ERROR: {e}", ERROR)

        threading.Thread(target=_do, daemon=True).start()

    def _root_mkdir(self):
        if not self.root_ssh:
            return
        name = self._ask_string("New remote folder (sudo)", "Folder name:")
        if not name:
            return
        path = self.root_cwd.rstrip("/") + "/" + name

        def _do():
            try:
                _, err, rc = self._root_ssh_cmd(f'mkdir -p {shlex.quote(path)}')
                if rc != 0:
                    self._log(f"[Root] mkdir failed: {err}", ERROR)
                else:
                    self._log(f"[Root] Folder created: {path}", SUCCESS)
                    self.after(0, self._root_remote_refresh)
            except Exception as e:
                self._log(f"[Root] ERROR: {e}", ERROR)

        threading.Thread(target=_do, daemon=True).start()

    def _root_upload(self):
        if not self.root_ssh:
            messagebox.showwarning("Not connected", "Connect in sudo mode first.")
            return
        sel = self.local_list.curselection()
        if not sel:
            messagebox.showwarning("Nothing selected", "Select a file.")
            return
        raw  = self.local_list.get(sel[0])
        name = raw.strip().removeprefix("📁").strip()
        lp   = os.path.join(self._local_cwd, name)
        tmp  = f"/tmp/_tt_upload_{name}"
        dest = self.root_cwd.rstrip("/") + "/" + name
        if os.path.isdir(lp):
            messagebox.showinfo("Info", "Folder upload not supported.")
            return

        def _do():
            try:
                self._log(f"[Root] Uploading {name} → {dest}...", ACCENT2)
                sftp = self.root_ssh.open_sftp()
                sftp.put(lp, tmp)
                sftp.close()
                _, err, rc = self._root_ssh_cmd(f'mv "{tmp}" "{dest}"')
                if rc != 0:
                    self._log(f"[Root] mv failed: {err}", ERROR)
                    return
                self._root_ssh_cmd(f'chmod 644 "{dest}"')
                self._log(f"[Root] ✓ {name}", SUCCESS)
                self.after(0, self._root_remote_refresh)
            except Exception as e:
                self._log(f"[Root] Upload error: {e}", ERROR)

        threading.Thread(target=_do, daemon=True).start()

    def _root_download(self):
        if not self.root_ssh:
            messagebox.showwarning("Not connected", "Connect in sudo mode first.")
            return
        sel = self.remote_list.curselection()
        if not sel:
            messagebox.showwarning("Nothing selected", "Select a file.")
            return
        raw = self.remote_list.get(sel[0])
        if raw.startswith("📁"):
            messagebox.showinfo("Info", "Folder download not supported.")
            return
        name = raw.strip()
        rp   = self.root_cwd.rstrip("/") + "/" + name
        tmp  = f"/tmp/_tt_dl_{name}"
        lp   = os.path.join(self._local_cwd, name)

        def _do():
            try:
                self._log(f"[Root] Downloading {name}...", ACCENT2)
                # Copy file to /tmp — no chmod, the file stays owned by whoever
                # created it. We only need read access to pull it via SFTP.
                _, err, rc = self._root_ssh_cmd(
                    f'cp {shlex.quote(rp)} {shlex.quote(tmp)}')
                if rc != 0:
                    # cp failed — try cat redirect as fallback (read-only access)
                    _, err2, rc2 = self._root_ssh_cmd(
                        f'cat {shlex.quote(rp)} > {shlex.quote(tmp)}')
                    if rc2 != 0:
                        self._log(f"[Root] Download failed: {err or err2}", ERROR)
                        return
                # Make the tmp file readable by the SSH user so SFTP can pull it
                self._root_ssh_cmd(f'chmod 644 {shlex.quote(tmp)} 2>/dev/null || true')
                sftp = self.root_ssh.open_sftp()
                sftp.get(tmp, lp)
                sftp.close()
                self._root_ssh_cmd(f'rm -f {shlex.quote(tmp)}')
                self._log(f"[Root] ✓ {name}", SUCCESS)
                self.after(0, self._local_refresh)
            except Exception as e:
                self._log(f"[Root] Download error: {e}", ERROR)

        threading.Thread(target=_do, daemon=True).start()

    # ══════════════════════════════════════════════════════════════════════
    # LOCAL PANE  (shared between SFTP and root modes)
    # ══════════════════════════════════════════════════════════════════════
    def _local_refresh(self):
        self.local_list.delete(0, "end")
        self.local_path_var.set(self._local_cwd)  # keep entry in sync
        try:
            entries = os.listdir(self._local_cwd)
            dirs  = sorted([e for e in entries
                            if os.path.isdir(os.path.join(self._local_cwd, e))
                            and self._should_show(e)])
            files = sorted([e for e in entries
                            if not os.path.isdir(os.path.join(self._local_cwd, e))
                            and self._should_show(e)])
            for d in dirs:
                ok = os.access(os.path.join(self._local_cwd, d), os.R_OK)
                self.local_list.insert("end", f"📁 {d}")
                self.local_list.itemconfig(
                    self.local_list.size() - 1,
                    fg=(ERROR if not ok else (MUTED if d.startswith(".") else TEXT)))
            for f in files:
                ok = os.access(os.path.join(self._local_cwd, f), os.R_OK)
                self.local_list.insert("end", f"     {f}")
                self.local_list.itemconfig(
                    self.local_list.size() - 1,
                    fg=(ERROR if not ok else (MUTED if f.startswith(".") else TEXT)))
        except PermissionError:
            self._log(f"[Local] Permission denied: {self._local_cwd}", ERROR)
        except Exception as e:
            self._log(f"[Local] Error: {e}", ERROR)

    def _local_up(self):
        parent = os.path.dirname(self._local_cwd)
        if parent != self._local_cwd:
            self._local_cwd = parent
            self._local_refresh()

    def _local_dblclick(self, _e):
        sel = self.local_list.curselection()
        if not sel:
            return
        name = self.local_list.get(sel[0]).strip().removeprefix("📁").strip()
        path = os.path.join(self._local_cwd, name)
        if os.path.isdir(path):
            self._local_cwd = path
            self._local_refresh()

    def _local_mkdir(self):
        name = self._ask_string("New local folder", "Folder name:")
        if not name:
            return
        try:
            os.makedirs(os.path.join(self._local_cwd, name), exist_ok=True)
            self._local_refresh()
        except Exception as e:
            self._log(f"[Local] mkdir error: {e}", ERROR)

    def _local_context_menu(self, event):
        idx = self.local_list.nearest(event.y)
        if idx < 0:
            return
        self.local_list.selection_clear(0, "end")
        self.local_list.selection_set(idx)
        self.local_list.activate(idx)
        raw    = self.local_list.get(idx)
        name   = raw.strip().removeprefix("📁").strip()
        is_dir = raw.strip().startswith("📁")
        path   = os.path.join(self._local_cwd, name)
        menu = tk.Menu(self, tearoff=0, bg=PANEL2, fg=TEXT,
                       activebackground=SEL_BG, activeforeground=TEXT,
                       font=FT_SMALL, bd=0, relief="flat")
        menu.add_command(label="✏  Rename",
                         command=lambda: self._local_rename(name, path))
        menu.add_separator()
        menu.add_command(label="✕  Delete",
                         foreground=ERROR, activeforeground=ERROR,
                         command=lambda: self._local_delete(name, path, is_dir))
        try:     menu.tk_popup(event.x_root, event.y_root)
        finally: menu.grab_release()

    def _local_rename(self, name, old_path):
        new_name = self._ask_string("Rename", "New name:", prefill=name)
        if not new_name or new_name == name:
            return
        try:
            os.rename(old_path, os.path.join(self._local_cwd, new_name))
            self._local_refresh()
        except Exception as e:
            self._log(f"[Local] Rename error: {e}", ERROR)

    def _local_delete(self, name, path, is_dir):
        if not messagebox.askyesno(
                "Confirm",
                f"Delete local {'folder' if is_dir else 'file'}:\n{path}?"):
            return
        try:
            shutil.rmtree(path) if is_dir else os.remove(path)
            self._local_refresh()
            self._log(f"[Local] Deleted: {name}", SUCCESS)
        except Exception as e:
            self._log(f"[Local] Delete error: {e}", ERROR)

    # ══════════════════════════════════════════════════════════════════════
    # TAB: TERMINAL — launches native SSH terminal window
    # ══════════════════════════════════════════════════════════════════════
    def _build_terminal(self, p):
        center = tk.Frame(p, bg=BG)
        center.place(relx=0.5, rely=0.5, anchor="center")

        tk.Label(center, text="⌨", font=("Segoe UI", 52),
                 bg=BG, fg=BORDER).pack()
        tk.Label(center, text="SSH Terminal",
                 font=("Segoe UI", 18, "bold"),
                 bg=BG, fg=TEXT2).pack(pady=(8, 4))
        tk.Label(center,
                 text="Opens a native terminal window connected to your SSH target.\n"
                      "Connection details are taken from the panel above.",
                 font=("Segoe UI", 10), bg=BG, fg=MUTED,
                 justify="center").pack()

        tk.Button(center, text="  ⌨  Launch Terminal  ",
                  font=("Segoe UI", 13, "bold"),
                  bg=ACCENT, fg="#0a1510",
                  activebackground="#27b589", activeforeground="#0a1510",
                  bd=0, padx=28, pady=14, cursor="hand2",
                  command=self._term_launch).pack(pady=(28, 0))

        self.term_status_lbl = tk.Label(center, text="",
                                        font=FT_LABEL, bg=BG, fg=MUTED)
        self.term_status_lbl.pack(pady=(12, 0))

        # Dummy attrs — keep _connect_active / _disconnect_active compatible
        self.term_channel   = None
        self.term_ssh       = None
        self.term_running   = False
        self.term_input_var = tk.StringVar()

    def _term_launch(self):
        """Open a native terminal window with ssh pre-filled."""
        conn = self._get_conn()
        if not conn:
            messagebox.showwarning("Not configured",
                                   "Fill in the connection details first.")
            return
        ip, port, user, pwd, key = conn

        ssh_args = ["ssh", "-p", str(port)]
        if key:
            ssh_args += ["-i", key]
        ssh_args.append(f"{user}@{ip}")
        ssh_cmd_str = " ".join(ssh_args)

        import subprocess
        try:
            if IS_WINDOWS:
                try:
                    subprocess.Popen(["wt.exe", "new-tab", "--"] + ssh_args)
                    self._term_status(f"Launched in Windows Terminal — {user}@{ip}")
                    return
                except FileNotFoundError:
                    pass
                subprocess.Popen(
                    ["cmd.exe", "/c", "start", "cmd.exe", "/k", ssh_cmd_str])
                self._term_status(f"Launched in cmd — {user}@{ip}")
            else:
                # Use xdg-open / the system default terminal via $TERMINAL env var,
                # falling back to x-terminal-emulator (Debian/Ubuntu update-alternatives)
                import shutil
                term_bin = (
                    os.environ.get("TERMINAL") or
                    shutil.which("x-terminal-emulator") or
                    shutil.which("xdg-terminal") or
                    "xterm"   # last resort — almost always present
                )
                subprocess.Popen([term_bin, "-e", ssh_cmd_str])
                self._term_status(f"Launched {term_bin} — {user}@{ip}")
            self._log(f"[Terminal] {ssh_cmd_str}", ACCENT2)
        except Exception as e:
            messagebox.showerror("Launch failed", str(e))
            self._log(f"[Terminal] Error: {e}", ERROR)

    def _term_status(self, msg):
        if hasattr(self, "term_status_lbl"):
            self.term_status_lbl.config(text=f"  {msg}", fg=SUCCESS)
        self._log(f"[Terminal] {msg}", SUCCESS)

    # Stubs — keep _connect_active / _disconnect_active compatible
    def _term_connect_silent(self): pass
    def _term_connect(self):        self._term_launch()
    def _term_disconnect(self):     pass
    def _term_send_raw(self, data): pass

if __name__ == "__main__":
    app = TwentyTools()
    app.mainloop()
