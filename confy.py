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
try:
	import yaml
except Exception:  # pragma: no cover - helpful error if PyYAML missing
	yaml = None
import os
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
	return base / "confy" / "registry.yaml"


def _ensure_yaml_available() -> None:
	if yaml is None:
		print("PyYAML is required for the registry. Install with: pip install pyyaml")
		raise SystemExit(3)


def load_registry() -> Dict[str, str]:
	_ensure_yaml_available()
	p = registry_path()
	if not p.exists():
		return {}
	try:
		data = yaml.safe_load(p.read_text())
		if not isinstance(data, dict):
			return {}
		return {str(k): str(v) for k, v in data.items()}
	except Exception:
		return {}


def save_registry(data: Dict[str, str]) -> None:
	_ensure_yaml_available()
	p = registry_path()
	p.parent.mkdir(parents=True, exist_ok=True)
	p.write_text(yaml.safe_dump(data, sort_keys=False))


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
		if c.exists():
			return c
	return None


def find_all_existing(name: str) -> List[Path]:
	return [c for c in candidate_paths(name) if c.exists()]


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
	elif not path.exists():
		if not path.parent.exists():
			path.parent.mkdir(parents=True, exist_ok=True)
		path.write_text("")
		cmd.append(str(path))
	else:
		cmd.append(str(cwd))

	try:
		return subprocess.call(cmd, cwd=str(cwd))
	except FileNotFoundError:
		print(f"Editor '{editor}' not found. Set $EDITOR or pass --editor.")
		return 2


def print_list(reg: Dict[str, str]) -> None:
	print("Registered locations:")
	if reg:
		for k, v in reg.items():
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


def register_target(reg: Dict[str, str], target: str, given_path: Optional[str]) -> None:
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

	reg[target] = str(p)
	save_registry(reg)
	print(f"Registered {target} -> {p}")


def main(argv: List[str] | None = None) -> int:
	p = argparse.ArgumentParser(description="Quickly open config files in your editor")
	p.add_argument("target", nargs="?", help="config target name (eg. fish, tmux)")
	p.add_argument("reg_path", nargs="?", help="path to register (used with --register)")
	p.add_argument("-e", "--editor", help="editor to use (overrides $EDITOR)")
	p.add_argument("--print-dir", action="store_true", help="print the directory for cd")
	p.add_argument("-l", "--list", action="store_true", help="list known and registered targets")
	p.add_argument("-r", "--register", action="store_true", help="register a target to a path")
	p.add_argument("--create", action="store_true", help="create the file if missing when opening")
	args = p.parse_args(argv)

	registry = load_registry()

	if args.list:
		print_list(registry)
		return 0

	if args.register:
		if not args.target:
			print("--register requires a target name (eg. -r nvim)")
			return 2
		register_target(registry, args.target, args.reg_path)
		return 0

	if not args.target:
		p.print_help()
		return 1

	target = args.target
	editor = choose_editor(args.editor)

	# Priority: registered -> detected existing -> error
	if target in registry:
		path = Path(registry[target]).expanduser()
	else:
		# Look for all existing candidates
		existing = find_all_existing(target)
		if not existing:
			print(f"Could not find config for '{target}' in standard locations. Use --register to save a path.")
			return 2
		if len(existing) == 1:
			path = existing[0]
		else:
			# Multiple existing locations — prompt the user to choose and register
			chosen = prompt_choose_path(existing)
			if chosen is None:
				print("No selection made; aborting.")
				return 1
			# register the chosen path for future runs
			registry[target] = str(chosen)
			try:
				save_registry(registry)
			except SystemExit:
				# save_registry will have printed an error about PyYAML
				pass
			path = chosen

	if args.print_dir:
		out_dir = path if path.is_dir() else path.parent
		print(str(out_dir))
		return 0

	if not path.exists():
		if not args.create:
			print(f"Config path {path} does not exist. Use --create to create it.")
			return 1

	return open_in_editor(editor, path)


if __name__ == "__main__":
	raise SystemExit(main())
