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
import stat as _stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REGISTRY_FILENAME = "confy.yaml"

DEFAULT_CONFIGS: Dict[str, List[Path]] = {
    "fish": [Path.home() / ".config" / "fish" / "config.fish"],
    "tmux": [Path.home() / ".tmux.conf"],
    "nvim": [
        Path.home() / ".config" / "nvim" / "init.vim",
        Path.home() / ".config" / "nvim" / "init.lua",
    ],
    "bash": [Path.home() / ".bashrc"],
    "zsh": [Path.home() / ".zshrc"],
    "git": [Path.home() / ".gitconfig"],
    "ssh": [Path.home() / ".ssh" / "config"],
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

    fd, tmp_path = tempfile.mkstemp(prefix="confy-", dir=str(path.parent))
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
        return {"paths": {}, "config": {}, "defaults": {}}

    data = _read_yaml(p)
    if not isinstance(data, dict):
        return {"paths": {}, "config": {}, "defaults": {}}

    # Ensure all expected sections exist
    result: Dict[str, Any] = {
        "paths": data.get("paths", {}),
        "config": data.get("config", {}),
        "defaults": data.get("defaults", {}),
    }

    # Normalise paths to strings
    if isinstance(result["paths"], dict):
        result["paths"] = {
            str(k): str(v) for k, v in result["paths"].items()
        }

    return result


def save_registry(registry: Dict[str, Any]) -> bool:
    """Save the registry dict to the YAML file.

    The dict should have the same structure as returned by load_registry().
    """
    # Clean up: remove empty sections
    data = {}
    for key in ("paths", "config", "defaults"):
        if registry.get(key):
            data[key] = registry[key]

    return _write_yaml(registry_path(), data)


# ---------------------------------------------------------------------------
# Path discovery
# ---------------------------------------------------------------------------


def candidate_paths(name: str) -> List[Path]:
    """Return a list of likely config paths for *name*, deduplicated."""
    name = name.strip()
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
    if name in DEFAULT_CONFIGS:
        candidates.extend(DEFAULT_CONFIGS[name])

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
    if name in DEFAULT_CONFIGS:
        for p in DEFAULT_CONFIGS[name]:
            basenames.append(p.name)
    basenames.extend([
        f".{name}rc", f".{name}", f"{name}.conf",
        f"{name}.local", f"{name}.conf.local", "config",
    ])
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
    keys = sorted(set(list(DEFAULT_CONFIGS.keys()) + list(paths.keys())))
    found = False
    for k in keys:
        existing = find_existing(k)
        if existing:
            print(f"  - {k}: {existing}")
            found = True
    if not found:
        print("  (none detected)")


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

    # Handle symlinks safely
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

    registry.setdefault("paths", {})[target] = str(p)
    if save_registry(registry):
        print(f"Registered {target} -> {p}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    # Refuse to run as root
    if os.geteuid() == 0:
        print("Refusing to run as root or under sudo. Run confy as a normal user.")
        return 1

    p = argparse.ArgumentParser(
        description="Quickly open config files in your editor"
    )
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

    # Handle reset before loading registry
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
