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
# No external YAML dependency; use the built-in simple loader/dumper only.
import os
import json
import stat as _stat
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional


DEFAULT_CONFIGS = {
	"fish": [Path.home() / ".config" / "fish" / "config.fish"],
	"tmux": [Path.home() / ".tmux.conf"],
	"nvim": [Path.home() / ".config" / "nvim" / "init.vim", Path.home() / ".config" / "nvim" / "init.lua"],
	"bash": [Path.home() / ".bashrc"],
	"zsh": [Path.home() / ".zshrc"],
	"git": [Path.home() / ".gitconfig"],
	"ssh": [Path.home() / ".ssh" / "config"],
}


def registry_path() -> Path:
	xdg = os.environ.get("XDG_CONFIG_HOME")
	base = Path(xdg) if xdg else Path.home() / ".config"
	return base / "confy" / "confy.conf"


def _simple_yaml_load(text: str) -> Dict[str, str]:
	"""Very small YAML-like loader for simple key: value mappings.

	Supports:
	  - lines of the form `key: value`
	  - quoted values with single or double quotes
	  - ignores blank lines and `#` comments
	Does NOT support nested structures, lists, anchors, or complex types.
	"""
	out: Dict[str, str] = {}
	for line in text.splitlines():
		s = line.strip()
		if not s:
			continue
		if s.startswith("#"):
			continue
		if ":" not in s:
			continue
		k, v = s.split(":", 1)
		key = k.strip()
		val = v.strip()
		if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
			val = val[1:-1]
			# unescape common backslash escapes so JSON stored as a quoted string is valid
			try:
				val = val.encode("utf-8").decode("unicode_escape")
			except Exception:
				pass
		out[key] = val
	return out


def _simple_yaml_dump(data: Dict[str, str]) -> str:
	lines: List[str] = []
	for k, v in data.items():
		# Quote if value contains special chars or leading/trailing spaces
		need_quote = (
			not v
			or v[0].isspace()
			or v[-1].isspace()
			or any(ch in v for ch in "#:{}[],&*?|")
			or "\n" in v
		)
		if need_quote:
			safe = v.replace('"', '\\"')
			lines.append(f"{k}: \"{safe}\"")
		else:
			lines.append(f"{k}: {v}")
	return "\n".join(lines) + "\n"


def load_registry() -> Dict[str, str]:
	p = registry_path()
	if not p.exists():
		return {}
	try:
		text = p.read_text()
		raw = _simple_yaml_load(text)
		out: Dict[str, Dict] = {}
		for k, v in raw.items():
			try:
				entry = json.loads(v)
				if isinstance(entry, dict):
					out[k] = entry
				else:
					out[k] = {"path": str(v)}
			except Exception:
				out[k] = {"path": str(v)}
		return out
	except Exception:
		return {}


def save_registry(data: Dict[str, str]) -> None:
	import tempfile
	p = registry_path()
	p.parent.mkdir(parents=True, exist_ok=True)
	# convert each entry (dict) to a JSON string value for storage
	raw: Dict[str, str] = {}
	for k, v in data.items():
		if isinstance(v, dict):
			raw[k] = json.dumps(v)
		else:
			raw[k] = str(v)
	text = _simple_yaml_dump(raw)
	# write atomically to temp file then replace, set permissions to 0600
	fd, tmp_path = tempfile.mkstemp(prefix="confy-", dir=str(p.parent))
	try:
		with os.fdopen(fd, "w") as f:
			f.write(text)
		os.replace(tmp_path, p)
		try:
			os.chmod(p, 0o600)
		except Exception:
			pass
	finally:
		if os.path.exists(tmp_path):
			try:
				os.unlink(tmp_path)
			except Exception:
				pass


def candidate_paths(name: str) -> List[Path]:
	name = name.strip()
	candidates: List[Path] = []
	if name in DEFAULT_CONFIGS:
		candidates.extend(DEFAULT_CONFIGS[name])

	# common fallbacks
	candidates.extend([
		Path.home() / f".{name}rc",
		Path.home() / f".{name}_config",
		Path.home() / ".config" / name / "config",
		Path.home() / ".config" / name,
		Path.home() / f".{name}",
	])
	# dedupe while preserving order
	seen = set()
	ordered: List[Path] = []
	for c in candidates:
		s = str(c)
		if s not in seen:
			seen.add(s)
			ordered.append(c)
	return ordered


def find_existing(name: str) -> Optional[Path]:
	for c in candidate_paths(name):
		for f in _expand_candidate_to_files(c, name):
			if f.exists():
				return f
	return None


def find_all_existing(name: str) -> List[Path]:
	out: List[Path] = []
	for c in candidate_paths(name):
		for f in _expand_candidate_to_files(c, name):
			if f.exists():
				out.append(f)
	return out


def _expand_candidate_to_files(c: Path, name: str) -> List[Path]:
	"""If c is a directory, return likely config files inside it (prefer .local); otherwise return [c]."""
	if not c.exists():
		return [c]
	if c.is_file():
		return [c]
	files: List[Path] = []
	try:
		for child in c.iterdir():
			if child.is_file():
				nm = child.name
				# prefer files that mention the target or have .local
				if name in nm or nm == "config" or nm.endswith(".local"):
					files.append(child)
	except Exception:
		files = []

	# also check common basenames inside the directory
	basenames: List[str] = []
	if name in DEFAULT_CONFIGS:
		for p in DEFAULT_CONFIGS[name]:
			basenames.append(p.name)
	basenames.extend([f".{name}rc", f".{name}", f"{name}.conf", f"{name}.local", f"{name}.conf.local", "config"])
	for b in basenames:
		p = c / b
		if p.exists() and p.is_file() and p not in files:
			files.append(p)

	# dedupe and prefer .local files first
	seen = set()
	ordered: List[Path] = []
	# .local first
	for p in files:
		if p.name.endswith(".local"):
			if str(p) not in seen:
				seen.add(str(p))
				ordered.append(p)
	for p in files:
		if not p.name.endswith(".local"):
			if str(p) not in seen:
				seen.add(str(p))
				ordered.append(p)
	return ordered if ordered else []


def prompt_choose_path(paths: List[Path]) -> Optional[Path]:
	if not paths:
		return None
	if not sys.stdin.isatty():
		# Non-interactive: don't prompt
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


def choose_editor(cli_editor: Optional[str]) -> str:
	if cli_editor:
		return cli_editor
	return os.environ.get("VISUAL") or os.environ.get("EDITOR") or "nano"


def open_in_editor(editor: str, path: Path) -> int:
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


def print_list(reg: Dict[str, str]) -> None:
	print("Registered locations:")
	if reg:
		for k, v in reg.items():
			if isinstance(v, dict):
				if v.get("is_symlink"):
					print(f" - {k}: {v.get('path')} -> {v.get('target')}")
				else:
					print(f" - {k}: {v.get('path')}")
			else:
				print(f" - {k}: {v}")
	else:
		print(" (none)")

	print("\nDetected defaults (existing files):")
	keys = sorted(set(list(DEFAULT_CONFIGS.keys()) + list(reg.keys())))
	found = False
	for k in keys:
		existing = find_existing(k)
		if existing:
			print(f" - {k}: {existing}")
			found = True
	if not found:
		print(" (none detected)")


def register_target(reg: Dict[str, str], target: str, given_path: Optional[str], allow_symlink: bool = False) -> None:
	# NOTE: this function signature will be updated to accept allow_symlink via closure of args
	if given_path:
		p = Path(given_path).expanduser()
	else:
		# try to find a candidate in cwd
		cwd = Path.cwd()
		# prefer files in cwd that match typical names
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
			# fallback to cwd
			p = cwd

	# SECURITY: only allow registering user-owned paths
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

	# Handle symlinks: require explicit opt-in via environment variable or caller
	if p.is_symlink():
		if not allow_symlink:
			print(f"Refusing to register symlink path for safety (use --allow-symlink to override): {p}")
			return
		# resolve target and validate ownership and location
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

		# target must be inside $HOME
		try:
			target_path.relative_to(Path.home())
		except Exception:
			print(f"Refusing to register symlink pointing outside home: {p} -> {target_path}")
			return

		if tstat.st_uid != euid:
			print(f"Refusing to register symlink whose target is not owned by current user: {p} -> {target_path}")
			return

		# avoid world-writable targets
		if bool(tstat.st_mode & _stat.S_IWOTH):
			print(f"Refusing to register symlink pointing to world-writable target: {p} -> {target_path}")
			return

		entry = {
			"path": str(p),
			"is_symlink": True,
			"target": str(target_path),
			"inode": int(tstat.st_ino),
			"dev": int(tstat.st_dev),
			"mtime": float(tstat.st_mtime),
		}
	else:
		entry = {
			"path": str(p),
			"is_symlink": False,
			"inode": int(st.st_ino),
			"dev": int(st.st_dev),
			"mtime": float(st.st_mtime),
		}

	reg[target] = entry
	save_registry(reg)
	print(f"Registered {target} -> {p}")


def main(argv: List[str] | None = None) -> int:
	# Refuse to run as root
	if os.geteuid() == 0:
		print("Refusing to run as root or under sudo. Run confy as a normal user.")
		return 1

	p = argparse.ArgumentParser(description="Quickly open config files in your editor")
	p.add_argument("target", nargs="?", help="config target name (eg. fish, tmux)")
	p.add_argument("reg_path", nargs="?", help="path to register (used with --register)")
	p.add_argument("-e", "--editor", help="editor to use (overrides $EDITOR)")
	p.add_argument("--print-dir", action="store_true", help="print the directory for cd")
	p.add_argument("-l", "--list", action="store_true", help="list known and registered targets")
	p.add_argument("-r", "--register", action="store_true", help="register a target to a path")
	p.add_argument("--allow-symlink", action="store_true", help="allow registering symlink paths (requires validation)")
	p.add_argument("--reset", action="store_true", help="wipe the confy registry file (confy.conf)")
	p.add_argument("-f", "--force", action="store_true", help="non-interactive: pick first candidate automatically")
	args = p.parse_args(argv)

	# handle reset before loading registry
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
			print("--register requires a target name (eg. -r nvim)")
			return 2
		register_target(registry, args.target, args.reg_path, args.allow_symlink)
		return 0

	if not args.target:
		p.print_help()
		return 1

	target = args.target
	editor = choose_editor(args.editor)

	# Priority: registered -> detected existing -> error
	if target in registry:
		entry = registry[target]
		if isinstance(entry, dict):
			path = Path(entry.get("path")).expanduser()
		else:
			path = Path(str(entry)).expanduser()
	else:
		# Look for all existing candidates
		existing = find_all_existing(target)
		if not existing:
			print(f"Could not find config for '{target}' in standard locations. Use --register to save a path.")
			return 2
		if len(existing) == 1:
			path = existing[0]
		else:
			# Prefer .local suffixes when present
			local_candidates = [p for p in existing if p.name.endswith('.local')]
			if local_candidates:
				pool = local_candidates
			else:
				pool = existing

			# Multiple existing locations — decide based on --force / non-interactive
			if args.force:
				chosen = pool[0]
			elif not sys.stdin.isatty():
				print("Multiple config locations found but no TTY to prompt. Use --force to auto-select or --register to pick one.")
				return 1
			else:
				chosen = prompt_choose_path(pool)

			if chosen is None:
				print("No selection made; aborting.")
				return 1

			# register the chosen path for future runs, allow symlink only if user passed flag
			register_target(registry, target, str(chosen), args.allow_symlink)
			entry = registry.get(target)
			if not entry:
				return 1
			path = Path(entry.get('path'))

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
		print(f"Config path {path} does not exist. Use --register to register an existing path.")
		return 1

	return open_in_editor(editor, path)


if __name__ == "__main__":
	raise SystemExit(main())
