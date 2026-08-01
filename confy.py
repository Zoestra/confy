#!/usr/bin/env python3
"""confy — quick access to common Linux config files.

Usage examples:
  confy tmux           # opens ~/.tmux.conf in $EDITOR
  confy fish --editor vim
  confy fish --print-dir # prints the directory to `cd` into

Note: a script cannot change the parent shell's cwd. Use
`cd "$(confy --print-dir fish)"` in your shell to change directory.
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import stat as _stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REGISTRY_FILENAME = "confy.yaml"

def _default_configs() -> Dict[str, List[Path]]:
    """Return the default config paths, resolved against the current home directory."""
    home = Path.home()
    return {
        "fish": [home / ".config" / "fish" / "config.fish"],
        "tmux": [home / ".tmux.conf"],
        "nvim": [
            home / ".config" / "nvim" / "init.vim",
            home / ".config" / "nvim" / "init.lua",
        ],
        "bash": [home / ".bashrc"],
        "zsh": [home / ".zshrc"],
        "git": [home / ".gitconfig"],
        "ssh": [home / ".ssh" / "config"],
        "confy": [home / ".config" / "confy" / "confy.yaml"],
    }

# Additional fallback patterns used when searching for config files
FALLBACK_PATTERNS = [
    ".{name}rc",
    ".{name}_config",
    ".config/{name}/config",
    ".config/{name}",
    ".{name}",
]


# ---------------------------------------------------------------------------
# Registry (YAML-based persistent config)
# ---------------------------------------------------------------------------


def registry_path() -> Path:
    """Return the path to the confy registry file."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "confy" / REGISTRY_FILENAME


def _read_yaml(path: Path) -> Any:
    """Safely read and parse a YAML file. Returns None on error."""
    try:
        with path.open("r") as f:
            return yaml.safe_load(f)
    except yaml.YAMLError as e:
        print(f"Warning: failed to parse YAML in {path}: {e}", file=sys.stderr)
        return None
    except OSError as e:
        print(f"Warning: failed to read {path}: {e}", file=sys.stderr)
        return None


def _write_yaml(path: Path, data: Any) -> bool:
    """Atomically write data as YAML to path with secure permissions. Returns True on success."""
    path.parent.mkdir(parents=True, exist_ok=True)

    try:
        fd, tmp_path = tempfile.mkstemp(prefix="confy-", dir=str(path.parent))
    except OSError as e:
        print(f"Error: failed to write {path}: {e}", file=sys.stderr)
        return False

    try:
        with os.fdopen(fd, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
        os.replace(tmp_path, path)
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
        return True
    except (OSError, yaml.YAMLError) as e:
        print(f"Error: failed to write {path}: {e}", file=sys.stderr)
        return False
    finally:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


def load_registry() -> Dict[str, Any]:
    """Load the registry from the YAML file.

    Returns a dict with the following structure:
        {
            "paths": {"target": "/path/to/config", ...},
            "config": {"editor": "vim", ...},
            "defaults": {"target": ["/path1", "/path2"], ...}
        }

    Missing keys default to empty dicts.
    """
    p = registry_path()
    if not p.exists():
        return {"paths": {}, "config": {}, "defaults": {}, "hosts": {}}

    data = _read_yaml(p)
    if not isinstance(data, dict):
        return {"paths": {}, "config": {}, "defaults": {}, "hosts": {}}

    # Ensure all expected sections exist
    result: Dict[str, Any] = {
        "paths": data.get("paths", {}),
        "config": data.get("config", {}),
        "defaults": data.get("defaults", {}),
        "hosts": data.get("hosts", {}),
    }

    # Normalise paths to strings
    if isinstance(result["paths"], dict):
        result["paths"] = {str(k): str(v) for k, v in result["paths"].items()}

    return result


def save_registry(registry: Dict[str, Any]) -> bool:
    """Save the registry dict to the YAML file.

    The dict should have the same structure as returned by load_registry().
    """
    # Clean up: remove empty sections
    data = {}
    for key in ("paths", "config", "defaults", "hosts"):
        if registry.get(key):
            data[key] = registry[key]

    return _write_yaml(registry_path(), data)


# ---------------------------------------------------------------------------
# Path discovery
# ---------------------------------------------------------------------------


def candidate_paths(name: str) -> List[Path]:
    """Return a list of likely config paths for *name*, deduplicated."""
    name = name.strip()
    # Security: reject names that could escape the home directory
    if "/" in name or name in (".", ".."):
        return []
    candidates: List[Path] = []

    # Check user-defined defaults from registry first
    registry = load_registry()
    user_defaults = registry.get("defaults", {})
    if isinstance(user_defaults, dict) and name in user_defaults:
        raw = user_defaults[name]
        if isinstance(raw, list):
            for entry in raw:
                candidates.append(Path(str(entry)).expanduser())
        elif isinstance(raw, str):
            candidates.append(Path(raw).expanduser())

    # Built-in defaults
    defaults = _default_configs()
    if name in defaults:
        candidates.extend(defaults[name])

    # Common fallback patterns
    for pattern in FALLBACK_PATTERNS:
        candidates.append(Path.home() / pattern.format(name=name))

    # Deduplicate while preserving order
    seen: set[str] = set()
    ordered: List[Path] = []
    for c in candidates:
        s = str(c)
        if s not in seen:
            seen.add(s)
            ordered.append(c)
    return ordered


def _expand_candidate_to_files(c: Path, name: str) -> List[Path]:
    """If *c* is a directory, return likely config files inside it; otherwise return [c]."""
    if not c.exists():
        return [c]
    if c.is_file():
        return [c]

    files: List[Path] = []
    try:
        for child in c.iterdir():
            if child.is_file():
                nm = child.name
                if name in nm or nm == "config" or nm.endswith(".local"):
                    files.append(child)
    except Exception:
        files = []

    # Also check common basenames inside the directory
    basenames: List[str] = []
    defaults = _default_configs()
    if name in defaults:
        for p in defaults[name]:
            basenames.append(p.name)
    basenames.extend(
        [
            f".{name}rc",
            f".{name}",
            f"{name}.conf",
            f"{name}.local",
            f"{name}.conf.local",
            "config",
        ]
    )
    for b in basenames:
        p = c / b
        if p.exists() and p.is_file() and p not in files:
            files.append(p)

    # Deduplicate; prefer .local files first
    seen: set[str] = set()
    ordered: List[Path] = []
    for p in files:
        if p.name.endswith(".local") and str(p) not in seen:
            seen.add(str(p))
            ordered.append(p)
    for p in files:
        if not p.name.endswith(".local") and str(p) not in seen:
            seen.add(str(p))
            ordered.append(p)
    return ordered


def find_existing(name: str) -> Optional[Path]:
    """Return the first existing config file for *name*, or None."""
    for c in candidate_paths(name):
        for f in _expand_candidate_to_files(c, name):
            if f.exists():
                return f
    return None


def find_all_existing(name: str) -> List[Path]:
    """Return all existing config files for *name*."""
    out: List[Path] = []
    for c in candidate_paths(name):
        for f in _expand_candidate_to_files(c, name):
            if f.exists():
                out.append(f)
    return out


# ---------------------------------------------------------------------------
# Interactive path selection
# ---------------------------------------------------------------------------


def prompt_choose_path(paths: List[Path]) -> Optional[Path]:
    """Let the user pick from a list of paths interactively."""
    if not paths:
        return None
    if not sys.stdin.isatty():
        return None

    print("Multiple config locations found. Choose one to use:")
    for i, p in enumerate(paths, start=1):
        print(f"  {i}) {p}")
    print("  q) cancel")

    while True:
        choice = input("Select number (or q): ").strip()
        if choice.lower() in ("q", "quit", "c", "cancel"):
            return None
        if choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(paths):
                return paths[idx - 1]
        print("Invalid choice. Please enter a number from the list or 'q' to cancel.")


# ---------------------------------------------------------------------------
# Editor helpers
# ---------------------------------------------------------------------------


def choose_editor(cli_editor: Optional[str]) -> str:
    """Return the editor to use, falling back to $VISUAL, $EDITOR, then 'nano'."""
    if cli_editor:
        return cli_editor

    # Check registry config
    registry = load_registry()
    reg_editor = registry.get("config", {}).get("editor")
    if reg_editor:
        return str(reg_editor)

    return os.environ.get("VISUAL") or os.environ.get("EDITOR") or "nano"


def open_in_editor(editor: str, path: Path) -> int:
    """Open *path* in *editor*. Returns the exit code."""
    cwd = path if path.is_dir() else path.parent
    cmd = [editor]
    if path.exists() and path.is_file():
        cmd.append(str(path))
    elif path.exists() and path.is_dir():
        cmd.append(str(path))
    else:
        print(f"Refusing to create or open non-existing path: {path}")
        return 1

    try:
        return subprocess.call(cmd, cwd=str(cwd))
    except FileNotFoundError:
        print(f"Editor '{editor}' not found. Set $EDITOR or pass --editor.")
        return 2


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def print_list(registry: Dict[str, Any]) -> None:
    """Print registered and detected config locations."""
    paths = registry.get("paths", {})
    config = registry.get("config", {})
    hosts = registry.get("hosts", {})

    if config:
        print("Configuration:")
        for k, v in config.items():
            print(f"  {k}: {v}")
        print()

    print("Registered paths:")
    if paths:
        for k, v in paths.items():
            print(f"  - {k}: {v}")
    else:
        print("  (none)")

    print("\nDetected defaults (existing files):")
    defaults = _default_configs()
    keys = sorted(set(list(defaults.keys()) + list(paths.keys())))
    found = False
    for k in keys:
        existing = find_existing(k)
        if existing:
            print(f"  - {k}: {existing}")
            found = True
    if not found:
        print("  (none detected)")

    if hosts:
        print("\nRemote hosts:")
        for name, info in hosts.items():
            conn = info.get("connection", "?")
            print(f"  - {name}: {conn}")
            programs = info.get("programs", {})
            if programs:
                for prog, pdata in programs.items():
                    path = pdata.get("path", "?")
                    files = pdata.get("files", [])
                    file_count = len(files)
                    print(f"      {prog} → {path} ({file_count} files)")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_target(
    registry: Dict[str, Any],
    target: str,
    given_path: Optional[str],
) -> None:
    """Register *target* to point at *given_path* (or auto-detect one)."""
    if given_path:
        p = Path(given_path).expanduser()
    else:
        cwd = Path.cwd()
        candidates = candidate_paths(target)
        match = None
        for c in candidates:
            alt = cwd / c.name
            if alt.exists():
                match = alt
                break
        if match:
            p = match
        else:
            p = cwd

    # Security: only allow registering user-owned paths
    euid = os.geteuid()
    try:
        exists = p.exists()
    except Exception:
        exists = False

    if not exists:
        print(f"Refusing to register non-existing path: {p}")
        return

    try:
        st = p.stat()
        owner = st.st_uid
    except Exception:
        print(f"Could not stat path: {p}")
        return

    if owner != euid:
        print(f"Refusing to register path not owned by current user: {p}")
        return

    # Handle symlinks safely — store the resolved target to avoid TOCTOU
    resolved_for_storage = str(p)
    if p.is_symlink():
        try:
            target_path = p.resolve(strict=True)
        except Exception:
            print(f"Symlink target does not exist or cannot be resolved: {p}")
            return

        try:
            tstat = target_path.stat()
        except Exception:
            print(f"Could not stat symlink target: {target_path}")
            return

        try:
            target_path.relative_to(Path.home())
        except Exception:
            print(
                f"Refusing to register symlink pointing outside home: "
                f"{p} -> {target_path}"
            )
            return

        if tstat.st_uid != euid:
            print(
                f"Refusing to register symlink whose target is not owned "
                f"by current user: {p} -> {target_path}"
            )
            return

        if bool(tstat.st_mode & _stat.S_IWOTH):
            print(
                f"Refusing to register symlink pointing to world-writable "
                f"target: {p} -> {target_path}"
            )
            return

        resolved_for_storage = str(target_path)

    registry.setdefault("paths", {})[target] = resolved_for_storage
    if save_registry(registry):
        print(f"Registered {target} -> {resolved_for_storage}")


# ---------------------------------------------------------------------------
# Backup: create timestamped copies of config files
# ---------------------------------------------------------------------------


def _backup_local_file(file_path: Path) -> Optional[Path]:
    """Create a backup of *file_path* in a .backups/ subdirectory.

    Copies file_path to .backups/<filename>.bak.N where N is the next
    available increment number determined by scanning existing backups.
    Returns the backup path or None on failure.
    """
    if not file_path.exists() or not file_path.is_file():
        print(f"Warning: not a regular file, skipping backup: {file_path}", file=sys.stderr)
        return None

    parent = file_path.parent
    backups_dir = parent / ".backups"

    try:
        backups_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(backups_dir, 0o700)
    except OSError as e:
        print(f"Warning: could not create backups dir {backups_dir}: {e}", file=sys.stderr)
        return None

    basename = file_path.name

    # Find the highest existing backup number
    highest = -1
    prefix = basename + ".bak."
    try:
        for entry in backups_dir.iterdir():
            if entry.is_file() and entry.name.startswith(prefix):
                suffix = entry.name[len(prefix):]
                if suffix.isdigit():
                    highest = max(highest, int(suffix))
    except OSError:
        pass

    next_n = highest + 1
    backup_name = f"{basename}.bak.{next_n}"
    backup_path = backups_dir / backup_name

    try:
        shutil.copy2(file_path, backup_path)
        os.chmod(backup_path, 0o600)
        print(f"  Backup: {file_path} → {backup_path}")
        return backup_path
    except OSError as e:
        print(f"  Warning: backup failed for {file_path}: {e}", file=sys.stderr)
        return None


def _backup_remote_files(
    connection: str,
    remote_path: str,
    filenames: List[str],
    extra_ssh_opts: Optional[List[str]] = None,
) -> int:
    """Backup files on a remote host by running a backup script via SSH.

    For each filename, creates a backup in
    <remote_path>/.backups/<filename>.bak.N on the remote.
    Returns 0 on success, non-zero on failure.
    """
    if not filenames:
        return 0

    safe_remote_path = shlex.quote(remote_path)
    safe_backup_dir = shlex.quote(f"{remote_path}/.backups")

    # Build a shell script that performs the backup for each file
    script_lines: List[str] = [
        f"mkdir -p {safe_backup_dir}",
        f"chmod 700 {safe_backup_dir}",
    ]

    for fname in filenames:
        safe_fname = shlex.quote(fname)
        safe_src = shlex.quote(f"{remote_path}/{fname}")
        # Bash variable expansion uses ${{...}} which Python f-string
        # represents as {{...}} to produce a literal ${...}
        script_lines.append(
            f'HIGHEST=-1; '
            f'for f in {safe_backup_dir}/{safe_fname}.bak.*; do '
            f'[ -f "$f" ] || continue; '
            f'num="${{f##*.}}"; '
            f'case "$num" in *[!0-9]*) continue ;; esac; '
            f'[ "$num" -gt "$HIGHEST" ] && HIGHEST=$num; '
            f'done; '
            f'NEXT=$((HIGHEST + 1)); '
            f'cp -p {safe_src} {safe_backup_dir}/{safe_fname}.bak.$NEXT; '
            f'chmod 600 {safe_backup_dir}/{safe_fname}.bak.$NEXT; '
            f'echo "  Backup: {fname} -> {fname}.bak.$NEXT"'
        )

    script = "; ".join(script_lines)

    ssh_opts = ["-o", "ConnectTimeout=10"]
    if extra_ssh_opts:
        ssh_opts = extra_ssh_opts + ssh_opts

    print(f"Backing up remote files on {connection}...")
    rc = subprocess.call(
        ["ssh", "-t", *ssh_opts, connection, "sudo", "bash", "-c", shlex.quote(script)],
        stdin=sys.stdin,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )
    return rc


# ---------------------------------------------------------------------------
# Push: copy config files to remote machines
# ---------------------------------------------------------------------------


@contextmanager
def ssh_master(connection: str):
    """Set up SSH connection multiplexing to avoid repeated password prompts.

    Creates a temporary control socket directory, starts a background
    SSH master connection, and yields extra SSH/scp options that make
    subsequent commands reuse the authenticated connection.

    The master connection and temporary directory are always torn down
    when the context exits, regardless of success or failure.
    """
    tmpdir = tempfile.mkdtemp(prefix="confy-ssh-")
    # Restrict permissions on the temp dir so only the owner can access
    # the control socket.
    try:
        os.chmod(tmpdir, 0o700)
    except Exception:
        pass
    control_path = os.path.join(tmpdir, "control.sock")

    extra_opts: List[str] = [
        "-o", f"ControlPath={control_path}",
        "-o", "ControlMaster=auto",
    ]

    try:
        # Start a background master connection.  The user may be prompted
        # for a password here — but only once for the entire push session.
        rc = subprocess.call(
            [
                "ssh",
                "-fN",                              # background, no command
                "-o", f"ControlPath={control_path}",
                "-o", "ControlMaster=yes",
                "-o", "ControlPersist=yes",         # stay alive until -O exit
                "-o", "ConnectTimeout=10",
                "-o", "BatchMode=no",
                connection,
            ],
            stdin=sys.stdin,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if rc == 0:
            yield extra_opts
        else:
            print(
                f"Warning: SSH master connection to {connection} failed (exit {rc}). "
                f"Each push will authenticate separately.",
                file=sys.stderr,
            )
            yield None
    finally:
        # Explicitly close the master connection.
        subprocess.call(
            [
                "ssh",
                "-o", f"ControlPath={control_path}",
                "-O", "exit",
                connection,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Remove the socket and temp directory.
        try:
            os.unlink(control_path)
        except Exception:
            pass
        try:
            os.rmdir(tmpdir)
        except Exception:
            pass


def _interactive_setup_push(registry: Dict[str, Any]) -> int:
    """Walk the user through setting up a new push target.

    Prompts for alias, connection (if new), program name, and remote path.
    Tests the SSH connection for new hosts, then stores under the new
    ``hosts.<alias>.programs.<program>`` structure.
    Credentials (passwords) are never stored.
    """
    if not sys.stdin.isatty():
        print("Interactive setup requires a TTY.", file=sys.stderr)
        return 1

    print("=== confy push: first-time setup ===\n")

    # 1. Alias for the target machine
    alias = input("Short name / alias for this machine (e.g. onosendai): ").strip()
    if not alias:
        print("Error: alias cannot be empty.", file=sys.stderr)
        return 1

    # Check if host already exists (allow adding programs to existing host)
    existing_host = registry.get("hosts", {}).get(alias)
    if existing_host:
        connection = existing_host.get("connection", "")
        print(f"Host '{alias}' already exists (connection: {connection}). Adding a new program.")
    else:
        connection = input("Remote connection (user@ip, e.g. zoe@192.168.1.42): ").strip()
        if not connection or "@" not in connection:
            print("Error: connection must be in the form user@ip.", file=sys.stderr)
            return 1

        # Test SSH connection
        print(f"\nTesting SSH connection to {connection} ...")
        rc = subprocess.call(
            ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=no", connection, "echo ok"],
            stdin=sys.stdin,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if rc != 0:
            print(
                f"Warning: SSH test to {connection} exited with code {rc}. "
                f"Proceeding anyway; the connection will be retried on each push.",
                file=sys.stderr,
            )

    # 2. Program name
    program = input("Program / config name (e.g. nix): ").strip()
    if not program:
        print("Error: program name cannot be empty.", file=sys.stderr)
        return 1

    # Check if program already exists for this host
    existing_programs = registry.get("hosts", {}).get(alias, {}).get("programs", {})
    if program in existing_programs:
        print(
            f"Program '{program}' already exists for host '{alias}'.",
            file=sys.stderr,
        )
        return 1

    # 3. Remote path
    remote_path = input(
        f"Remote path to place {program} configs on '{alias}' "
        f"(e.g. /etc/nixos/): "
    ).strip()
    if not remote_path:
        print("Error: remote path cannot be empty.", file=sys.stderr)
        return 1

    # Store: hosts.<alias>.programs.<program> = {path, files}
    host_data = registry.setdefault("hosts", {}).setdefault(alias, {})
    if not existing_host:
        host_data["connection"] = connection
    host_data.setdefault("programs", {})[program] = {
        "path": remote_path,
        "files": [],
    }

    if save_registry(registry):
        print(f"\n✓ Stored: push {program} → {connection}:{remote_path} (host: {alias})")
        print(f"  You can now run: confy push {alias} {program} --add <file>")
        return 0
    else:
        print("Error: failed to save registry.", file=sys.stderr)
        return 1


def _push_file(
    host: str,
    program: str,
    filename: str,
    registry: Dict[str, Any],
    extra_ssh_opts: Optional[List[str]] = None,
    no_bak: bool = False,
) -> int:
    """Push *filename* to the remote machine for *program* on *host*.

    Looks up connection from hosts.<host>.connection and remote path from
    hosts.<host>.programs.<program>.path.  Copies via scp.
    If the direct scp fails with a permission error it retries with a
    sudo-aware approach (scp to /tmp then sudo mv on the remote side).
    """
    hosts = registry.get("hosts", {})

    # Resolve host
    host_info = hosts.get(host)
    if host_info is None:
        print(
            f"No host registered for '{host}'. "
            f"Run 'confy push --new' to set one up.",
            file=sys.stderr,
        )
        return 1

    connection = host_info.get("connection", "")
    if not connection:
        print(f"Host '{host}' has no connection string.", file=sys.stderr)
        return 1

    # Resolve program
    programs = host_info.get("programs", {})
    prog_info = programs.get(program)
    if prog_info is None:
        print(
            f"No program '{program}' registered for host '{host}'. "
            f"Run 'confy push --new' to set one up.",
            file=sys.stderr,
        )
        return 1

    remote_path = prog_info.get("path", "").rstrip("/")
    if not remote_path:
        print(f"Program '{program}' on host '{host}' has no remote path.", file=sys.stderr)
        return 1

    # Validate remote path to prevent path traversal
    if not remote_path.startswith("/"):
        print(f"Error: remote path must be absolute for {host}/{program}.", file=sys.stderr)
        return 1
    if ".." in Path(remote_path).parts:
        print(f"Error: remote path traversal detected for {host}/{program}.", file=sys.stderr)
        return 1

    # Resolve local file
    local_file = Path(filename).expanduser().resolve()
    if not local_file.exists():
        print(f"Local file not found: {local_file}", file=sys.stderr)
        return 1
    if not local_file.is_file():
        print(f"Not a regular file: {local_file}", file=sys.stderr)
        return 1

    dest = f"{connection}:{remote_path}/{local_file.name}"
    ssh_opts = ["-o", "ConnectTimeout=10"]
    if extra_ssh_opts:
        ssh_opts = extra_ssh_opts + ssh_opts

    # Backup the remote file before overwriting
    if not no_bak:
        _backup_remote_files(connection, remote_path, [local_file.name], extra_ssh_opts)

    print(f"Pushing {local_file} → {dest}")

    # --- Attempt 1: direct scp ---
    rc = subprocess.call(
        ["scp", *ssh_opts, str(local_file), dest],
        stdin=sys.stdin,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )
    if rc == 0:
        print("✓ Done.")
        return 0

    # --- Attempt 2: scp to /tmp, then sudo mv on remote ---
    print("\nDirect scp failed — retrying with sudo on the remote side …")
    tmp_dest = f"{connection}:/tmp/{local_file.name}"
    rc2 = subprocess.call(
        ["scp", *ssh_opts, str(local_file), tmp_dest],
        stdin=sys.stdin,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )
    if rc2 != 0:
        print("Error: scp to /tmp also failed.", file=sys.stderr)
        return rc2

    # Now sudo mv on the remote (shell-quoted to prevent injection)
    mv_cmd = "sudo mv /tmp/{} {}/{}".format(
        shlex.quote(str(local_file.name)),
        shlex.quote(remote_path),
        shlex.quote(str(local_file.name)),
    )
    rc3 = subprocess.call(
        ["ssh", "-t", *ssh_opts, connection, "bash", "-c", shlex.quote(mv_cmd)],
        stdin=sys.stdin,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )
    if rc3 == 0:
        print("✓ Done (via sudo).")
        return 0
    else:
        print(
            f"Error: sudo mv on remote failed (exit {rc3}). "
            f"File left at /tmp/{local_file.name} on the remote.",
            file=sys.stderr,
        )
        return rc3


def _push_batch_add(
    host: str,
    program: str,
    filename: str,
    registry: Dict[str, Any],
) -> int:
    """Add *filename* to the batch for *program* on *host*.

    Resolves the path to an absolute path and stores it in the registry.
    Does NOT push the file.
    """
    hosts = registry.get("hosts", {})

    host_info = hosts.get(host)
    if host_info is None:
        print(
            f"No host registered for '{host}'. Run 'confy push --new' first.",
            file=sys.stderr,
        )
        return 1

    programs = host_info.get("programs", {})
    if program not in programs:
        print(
            f"No program '{program}' registered for host '{host}'. "
            f"Run 'confy push --new' first.",
            file=sys.stderr,
        )
        return 1

    file_path = str(Path(filename).expanduser().resolve())

    files = programs[program].setdefault("files", [])

    if file_path in files:
        print(f"File already in batch [{host}/{program}]: {file_path}")
        return 0

    files.append(file_path)

    if save_registry(registry):
        print(f"Added to batch [{host}/{program}]: {file_path}")
        return 0
    else:
        print("Error: failed to save registry.", file=sys.stderr)
        return 1


def _push_batch_remove(
    host: str,
    program: str,
    filename: str,
    registry: Dict[str, Any],
) -> int:
    """Remove *filename* from the batch for *program* on *host*."""
    hosts = registry.get("hosts", {})

    host_info = hosts.get(host)
    if host_info is None:
        print(f"No host registered for '{host}'.", file=sys.stderr)
        return 1

    programs = host_info.get("programs", {})
    if program not in programs:
        print(f"No program '{program}' registered for host '{host}'.", file=sys.stderr)
        return 1

    files = programs[program].get("files", [])
    file_path = str(Path(filename).expanduser().resolve())

    if file_path not in files:
        print(f"File not in batch [{host}/{program}]: {filename}", file=sys.stderr)
        return 1

    files.remove(file_path)

    if save_registry(registry):
        print(f"Removed from batch [{host}/{program}]: {file_path}")
        return 0
    else:
        print("Error: failed to save registry.", file=sys.stderr)
        return 1


def _push_batch_list(
    host: str,
    program: Optional[str],
    registry: Dict[str, Any],
) -> int:
    """List all files in the batch for *host* (and optionally *program*)."""
    hosts = registry.get("hosts", {})

    host_info = hosts.get(host)
    if host_info is None:
        print(f"No host registered for '{host}'.", file=sys.stderr)
        return 1

    programs = host_info.get("programs", {})

    if program:
        if program not in programs:
            print(f"No program '{program}' registered for host '{host}'.", file=sys.stderr)
            return 1
        files = programs[program].get("files", [])
        conn = host_info.get("connection", "?")
        path = programs[program].get("path", "?")
        print(f"Batch for {host} ({conn}) / {program} → {path}:")
        if files:
            for f in files:
                print(f"  {f}")
        else:
            print("  (empty)")
    else:
        conn = host_info.get("connection", "?")
        print(f"Batch for {host} ({conn}):")
        found_any = False
        for prog_name, prog_info in programs.items():
            files = prog_info.get("files", [])
            path = prog_info.get("path", "?")
            print(f"  [{prog_name}] → {path} ({len(files)} files)")
            for f in files:
                print(f"    {f}")
            if files:
                found_any = True
        if not found_any:
            print("  (no files in any batch)")
    return 0


def _push_all(
    host: str,
    program: Optional[str],
    registry: Dict[str, Any],
    extra_ssh_opts: Optional[List[str]] = None,
    no_bak: bool = False,
) -> int:
    """Push all batch files for *host* (and optionally *program*).

    If *program* is given, pushes only that program's batch.
    Otherwise pushes all programs' batches for the host.

    Files that need sudo (direct scp fails with permission error) are
    collected and handled together in a single ``ssh -t`` call so the
    user only has to type the sudo password once.
    """
    hosts = registry.get("hosts", {})

    host_info = hosts.get(host)
    if host_info is None:
        print(
            f"No host registered for '{host}'. Run 'confy push --new' first.",
            file=sys.stderr,
        )
        return 1

    connection = host_info.get("connection", "")
    if not connection:
        print(f"Host '{host}' has no connection string.", file=sys.stderr)
        return 1

    programs = host_info.get("programs", {})
    if not programs:
        print(
            f"No programs registered for host '{host}'. "
            f"Run 'confy push --new' first.",
            file=sys.stderr,
        )
        return 1

    # --- collect items to push: (local_path, prog_name, remote_path) ---
    items: List[tuple[Path, str, str]] = []

    if program:
        if program not in programs:
            print(f"No program '{program}' registered for host '{host}'.", file=sys.stderr)
            return 1
        prog_info = programs[program]
        remote_path = _validate_remote_path(host, program, prog_info)
        if remote_path is None:
            return 1
        files = prog_info.get("files", [])
        if not files:
            print(f"No files in batch for [{host}/{program}]. Use --add to add files.")
            return 1
        for f in files:
            local_file = Path(f).expanduser().resolve()
            if local_file.is_file():
                items.append((local_file, program, remote_path))
            else:
                print(f"Warning: skipping {f} — not a regular file.", file=sys.stderr)
    else:
        for prog_name, prog_info in programs.items():
            files = prog_info.get("files", [])
            if not files:
                continue
            remote_path = _validate_remote_path(host, prog_name, prog_info)
            if remote_path is None:
                continue
            for f in files:
                local_file = Path(f).expanduser().resolve()
                if local_file.is_file():
                    items.append((local_file, prog_name, remote_path))
                else:
                    print(f"Warning: skipping {f} — not a regular file.", file=sys.stderr)

    if not items:
        print("No valid files in any batch. Use --add to add files.")
        return 1

    ssh_opts = ["-o", "ConnectTimeout=10"]
    if extra_ssh_opts:
        ssh_opts = extra_ssh_opts + ssh_opts

    # --- Backup remote files before pushing ---
    if not no_bak:
        # Group items by (program, remote_path) for efficient SSH usage
        backup_groups: Dict[tuple[str, str], List[str]] = {}
        for _l, prog_name, rp in items:
            key = (prog_name, rp)
            backup_groups.setdefault(key, []).append(_l.name)

        for (_prog, rp), fnames in backup_groups.items():
            _backup_remote_files(connection, rp, fnames, extra_ssh_opts)

    total = len(items)
    print(f"\n=== Pushing {total} file(s) for {host} ===\n")

    # ---- Phase 1: try direct scp for every file ----
    sudo_needed: List[tuple[Path, str, str]] = []  # (local, prog, remote_path)
    ok = 0

    for local_file, _prog, remote_path in items:
        dest = f"{connection}:{remote_path}/{local_file.name}"
        print(f"Pushing {local_file} → {dest}")
        rc = subprocess.call(
            ["scp", *ssh_opts, str(local_file), dest],
            stdin=sys.stdin,
            stdout=sys.stdout,
            stderr=sys.stderr,
        )
        if rc == 0:
            print("✓ Done.")
            ok += 1
        else:
            sudo_needed.append((local_file, _prog, remote_path))

    # ---- Phase 2: handle all permission-denied files together ----
    if sudo_needed:
        print(f"\nDirect scp failed for {len(sudo_needed)} file(s). "
              f"Retrying with sudo (one password prompt) …\n")

        # 2a. scp each file to /tmp on the remote
        tmp_ok: List[tuple[Path, str, str]] = []
        for local_file, _prog, remote_path in sudo_needed:
            tmp_dest = f"{connection}:/tmp/{local_file.name}"
            rc = subprocess.call(
                ["scp", *ssh_opts, str(local_file), tmp_dest],
                stdin=sys.stdin,
                stdout=sys.stdout,
                stderr=sys.stderr,
            )
            if rc == 0:
                tmp_ok.append((local_file, _prog, remote_path))
            else:
                print(f"Error: scp to /tmp failed for {local_file}", file=sys.stderr)

        # 2b. single ssh -t to sudo mv all the files at once
        if tmp_ok:
            mv_cmds = []
            for local_file, _prog, remote_path in tmp_ok:
                mv_cmds.append(
                    "sudo mv /tmp/{} {}/{}".format(
                        shlex.quote(str(local_file.name)),
                        shlex.quote(remote_path),
                        shlex.quote(str(local_file.name)),
                    )
                )
            combined = " && ".join(mv_cmds)
            rc = subprocess.call(
                [
                    "ssh", "-t", *ssh_opts, connection,
                    "bash", "-c", shlex.quote(combined),
                ],
                stdin=sys.stdin,
                stdout=sys.stdout,
                stderr=sys.stderr,
            )
            if rc == 0:
                print("✓ All sudo moves succeeded.")
                ok += len(tmp_ok)
            else:
                print(
                    f"Error: sudo mv batch failed (exit {rc}). "
                    f"Files may remain in /tmp/ on the remote.",
                    file=sys.stderr,
                )

    failed = total - ok
    if failed == 0:
        print(f"\n✓ All {total} files pushed successfully.")
    else:
        print(f"\n✗ {failed}/{total} files failed to push.")
    return 1 if failed > 0 else 0


def _validate_remote_path(host: str, program: str, prog_info: Dict[str, Any]) -> Optional[str]:
    """Validate and return the remote path for a program, or None if invalid."""
    remote_path = prog_info.get("path", "").rstrip("/")
    if not remote_path:
        print(f"Program '{program}' on host '{host}' has no remote path.", file=sys.stderr)
        return None
    if not remote_path.startswith("/"):
        print(f"Error: remote path must be absolute for {host}/{program}.", file=sys.stderr)
        return None
    if ".." in Path(remote_path).parts:
        print(f"Error: remote path traversal detected for {host}/{program}.", file=sys.stderr)
        return None
    return remote_path


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _parse_backup_args(argv: List[str]) -> argparse.Namespace:
    """Parse arguments for the 'backup' subcommand.

    Syntax:
        confy backup <program> [file...]           # local backup
        confy backup <host> <program> [file...]    # remote backup
    """
    p = argparse.ArgumentParser(
        prog="confy backup",
        description="Backup config files (local or remote)",
    )
    p.add_argument("arg1", nargs="?", help="program name or host alias")
    p.add_argument("arg2", nargs="?", help="program name or file path")
    p.add_argument("arg3", nargs="*", help="file path(s)")
    return p.parse_args(argv)


def _backup_context_files(registry: Dict[str, Any]) -> int:
    """Back up all registered local files inside the current working directory.

    Scans registry["paths"] for registered config files whose resolved
    path is in or under ``Path.cwd()`` and creates local backups of each
    one.  Remote batch files are not touched — use ``confy backup <host>
    <program>`` for remote backups.

    Returns 0 if all backups succeeded, 1 if any failed, or 1 if no
    registered files were found in the current directory.
    """
    cwd = Path.cwd().resolve()

    local_files: List[Path] = []

    # --- Local paths ---
    paths = registry.get("paths", {})
    if isinstance(paths, dict):
        for target, raw_path in paths.items():
            p = Path(str(raw_path)).expanduser().resolve()
            try:
                p.relative_to(cwd)
            except ValueError:
                continue  # not under cwd
            if p.is_file():
                local_files.append(p)

    # --- Hosts batch files (local copies, not remote backups) ---
    hosts = registry.get("hosts", {})
    if isinstance(hosts, dict):
        for host_info in hosts.values():
            if not isinstance(host_info, dict):
                continue
            programs = host_info.get("programs", {})
            if not isinstance(programs, dict):
                continue
            for prog_info in programs.values():
                if not isinstance(prog_info, dict):
                    continue
                for bf in prog_info.get("files", []):
                    p = Path(str(bf)).expanduser().resolve()
                    try:
                        p.relative_to(cwd)
                    except ValueError:
                        continue
                    if p.is_file() and str(p) not in {str(f) for f in local_files}:
                        local_files.append(p)

    if not local_files:
        print(f"No registered files found under {cwd}", file=sys.stderr)
        return 1

    failed = 0

    print(f"Backing up {len(local_files)} local file(s) under {cwd}:")
    for f in local_files:
        if _backup_local_file(f) is None:
            failed += 1

    return 1 if failed > 0 else 0


def _handle_backup(
    registry: Dict[str, Any],
    argv: List[str],
) -> int:
    """Handle the 'backup' subcommand.

    Dispatches to local or remote backup based on whether the first
    argument matches a known host alias in the registry.

    When called with no arguments, scans the registry for registered
    files inside the current working directory and backs them all up.
    """
    args = _parse_backup_args(argv)

    if not args.arg1:
        return _backup_context_files(registry)

    hosts = registry.get("hosts", {})

    if args.arg1 in hosts:
        # Remote backup: confy backup <host> <program> [file...]
        host = args.arg1
        if not args.arg2:
            print(
                "Remote backup requires: confy backup <host> <program> [file...]",
                file=sys.stderr,
            )
            return 1
        program = args.arg2
        filenames: List[str] = []

        if args.arg3:
            filenames = args.arg3
        else:
            # Backup all batch files for this program
            prog_info = hosts[host].get("programs", {}).get(program)
            if not prog_info:
                print(
                    f"No program '{program}' registered for host '{host}'.",
                    file=sys.stderr,
                )
                return 1
            batch_files = prog_info.get("files", [])
            if not batch_files:
                print(
                    f"No files registered for [{host}/{program}].",
                    file=sys.stderr,
                )
                return 1
            filenames = [Path(f).name for f in batch_files]

        return _remote_backup_handler(host, program, filenames, registry)
    else:
        # Local backup: confy backup <program> [file...]
        program = args.arg1
        files: List[str] = []

        if args.arg2:
            if args.arg3:
                files = [args.arg2] + args.arg3
            else:
                files = [args.arg2]
        else:
            # No file specified — backup all existing config files for the program
            existing = find_all_existing(program)
            if not existing:
                print(
                    f"No config files found for '{program}'.",
                    file=sys.stderr,
                )
                return 1
            print(f"Backing up configs for '{program}':")
            failed = 0
            for f in existing:
                if _backup_local_file(f) is None:
                    failed += 1
            return 1 if failed > 0 else 0

        # Backup specific file(s)
        print(f"Backing up files for '{program}':")
        failed = 0
        for f in files:
            path = Path(f).expanduser().resolve()
            if _backup_local_file(path) is None:
                failed += 1
        return 1 if failed > 0 else 0


def _remote_backup_handler(
    host: str,
    program: str,
    filenames: List[str],
    registry: Dict[str, Any],
) -> int:
    """Run backup on a remote host for the given program and filenames.

    Looks up the connection string and remote path from the registry,
    then runs the backup via SSH.
    """
    hosts = registry.get("hosts", {})
    host_info = hosts.get(host)
    if not host_info:
        print(f"No host registered for '{host}'.", file=sys.stderr)
        return 1

    connection = host_info.get("connection", "")
    if not connection:
        print(f"Host '{host}' has no connection string.", file=sys.stderr)
        return 1

    prog_info = host_info.get("programs", {}).get(program)
    if not prog_info:
        print(
            f"No program '{program}' registered for host '{host}'.",
            file=sys.stderr,
        )
        return 1

    remote_path = _validate_remote_path(host, program, prog_info)
    if remote_path is None:
        return 1

    if not filenames:
        print(f"No files to backup for [{host}/{program}].", file=sys.stderr)
        return 1

    print(f"Backing up [{host}/{program}]:")
    return _backup_remote_files(connection, remote_path, filenames)


def _parse_push_args(argv: List[str]) -> argparse.Namespace:
    """Parse arguments for the 'push' subcommand.

    Syntax:
        confy push <host> [program] [file] [options]
        confy push --new                           # interactive setup
    """
    p = argparse.ArgumentParser(prog="confy push", description="Push config files to remote machines")
    p.add_argument("host", nargs="?", help="remote machine alias (e.g. onosendai)")
    p.add_argument("program", nargs="?", help="program / config name (e.g. nix)")
    p.add_argument("file", nargs="*", help="local file(s) to push / add / remove")
    p.add_argument(
        "-n", "--new", action="store_true", help="interactive first-time setup"
    )
    p.add_argument(
        "--all", action="store_true",
        help="push all batch files for host[/program]",
    )
    p.add_argument(
        "--add", action="store_true",
        help="add FILE to the batch without pushing",
    )
    p.add_argument(
        "--remove", action="store_true",
        help="remove FILE from the batch",
    )
    p.add_argument(
        "--list", dest="list_files", action="store_true",
        help="list batch files for host[/program]",
    )
    p.add_argument(
        "--no-bak", dest="no_bak", action="store_true",
        help="skip backup before pushing",
    )
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    # Refuse to run as root
    if os.geteuid() == 0:
        print("Refusing to run as root or under sudo. Run confy as a normal user.")
        return 1

    if argv is None:
        argv = sys.argv[1:]

    # --- Route to push subcommand ---
    if argv and argv[0] == "push":
        push_args = _parse_push_args(argv[1:])

        if push_args.new:
            registry = load_registry()
            return _interactive_setup_push(registry)

        if not push_args.host:
            _parse_push_args(["--help"])
            return 1

        registry = load_registry()

        # Resolve connection string for SSH multiplexing
        hosts_info = registry.get("hosts", {})
        host_entry = hosts_info.get(push_args.host, {})
        connection = host_entry.get("connection", "")

        # --all: push all batch files
        if push_args.all:
            if connection:
                with ssh_master(connection) as extra_ssh_opts:
                    return _push_all(
                        push_args.host, push_args.program, registry, extra_ssh_opts,
                        no_bak=push_args.no_bak,
                    )
            return _push_all(push_args.host, push_args.program, registry,
                           no_bak=push_args.no_bak)

        # --add: add files to batch (no push)
        if push_args.add:
            if not push_args.program or not push_args.file:
                print(
                    "--add requires host, program, and at least one file.",
                    file=sys.stderr,
                )
                return 1
            failed = 0
            for f in push_args.file:
                rc = _push_batch_add(
                    push_args.host, push_args.program, f, registry
                )
                if rc != 0:
                    failed += 1
            return 1 if failed > 0 else 0

        # --remove: remove files from batch
        if push_args.remove:
            if not push_args.program or not push_args.file:
                print(
                    "--remove requires host, program, and at least one file.",
                    file=sys.stderr,
                )
                return 1
            failed = 0
            for f in push_args.file:
                rc = _push_batch_remove(
                    push_args.host, push_args.program, f, registry
                )
                if rc != 0:
                    failed += 1
            return 1 if failed > 0 else 0

        # --list: list batch files
        if push_args.list_files:
            return _push_batch_list(
                push_args.host, push_args.program, registry
            )

        # Default: single-file push
        if not push_args.program or len(push_args.file) != 1:
            _parse_push_args(["--help"])
            return 1
        if connection:
            with ssh_master(connection) as extra_ssh_opts:
                return _push_file(
                    push_args.host, push_args.program, push_args.file[0], registry, extra_ssh_opts,
                    no_bak=push_args.no_bak,
                )
        return _push_file(
            push_args.host, push_args.program, push_args.file[0], registry,
            no_bak=push_args.no_bak,
        )

    # --- Route to backup subcommand ---
    if argv and argv[0] == "backup":
        registry = load_registry()
        return _handle_backup(registry, argv[1:])

    p = argparse.ArgumentParser(description="Quickly open config files in your editor")
    p.add_argument("target", nargs="?", help="config target name (e.g. fish, tmux)")
    p.add_argument(
        "reg_path", nargs="?", help="path to register (used with --register)"
    )
    p.add_argument("-e", "--editor", help="editor to use (overrides $EDITOR)")
    p.add_argument(
        "--print-dir",
        action="store_true",
        help="print the directory for cd",
    )
    p.add_argument(
        "-l", "--list", action="store_true", help="list known and registered targets"
    )
    p.add_argument(
        "-r",
        "--register",
        action="store_true",
        help="register a target to a path",
    )
    p.add_argument(
        "--reset",
        action="store_true",
        help="wipe the confy registry file (confy.yaml)",
    )
    p.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="non-interactive: pick first candidate automatically",
    )
    args = p.parse_args(argv)
    if args.reset:
        pth = registry_path()
        if pth.exists():
            try:
                pth.unlink()
                print(f"Removed registry file: {pth}")
            except Exception as e:
                print(f"Failed to remove registry file: {e}")
                return 1
        else:
            print(f"No registry file to remove at: {pth}")
        return 0

    registry = load_registry()

    if args.list:
        print_list(registry)
        return 0

    if args.register:
        if not args.target:
            print("--register requires a target name (e.g. -r nvim)")
            return 2
        register_target(registry, args.target, args.reg_path)
        return 0

    if not args.target:
        p.print_help()
        return 1

    target = args.target
    editor = choose_editor(args.editor)

    # Hardcoded: "confy" always opens the registry YAML file itself
    if target == "confy":
        path = registry_path()
    else:
        # Priority: registered -> detected existing -> error
        paths = registry.get("paths", {})
        if target in paths:
            path = Path(str(paths[target])).expanduser()
        else:
            existing = find_all_existing(target)
            if not existing:
                print(
                    f"Could not find config for '{target}' in standard locations. "
                    f"Use --register to save a path."
                )
                return 2

            if len(existing) == 1:
                path = existing[0]
            else:
                local_candidates = [p for p in existing if p.name.endswith(".local")]
                pool = local_candidates if local_candidates else existing

                if args.force:
                    chosen = pool[0]
                elif not sys.stdin.isatty():
                    print(
                        "Multiple config locations found but no TTY to prompt. "
                        "Use --force to auto-select or --register to pick one."
                    )
                    return 1
                else:
                    chosen = prompt_choose_path(pool)

                if chosen is None:
                    print("No selection made; aborting.")
                    return 1

                register_target(registry, target, str(chosen))
                paths = registry.get("paths", {})
                if target not in paths:
                    return 1
                path = Path(str(paths[target]))

    if args.print_dir:
        out_dir = path if path.is_dir() else path.parent
        print(str(out_dir))
        return 0

    # Security: refuse to touch root-owned paths when not running as root
    euid = os.geteuid()
    is_root = euid == 0
    try:
        st = path.stat()
        owner = st.st_uid
    except Exception:
        print(f"Could not stat path: {path}")
        return 1

    if not is_root and owner == 0:
        print(f"Refusing to open root-owned config {path} when not running as root.")
        return 1

    if not path.exists():
        print(
            f"Config path {path} does not exist. "
            f"Use --register to register an existing path."
        )
        return 1

    return open_in_editor(editor, path)


if __name__ == "__main__":
    raise SystemExit(main())
