"""Tests for confy.py."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Generator
from unittest.mock import patch

import pytest

from confy import (
    _read_yaml,
    _write_yaml,
    _expand_candidate_to_files,
    candidate_paths,
    choose_editor,
    find_all_existing,
    find_existing,
    load_registry,
    main,
    open_in_editor,
    print_list,
    prompt_choose_path,
    register_target,
    registry_path,
    save_registry,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[Path, None, None]:
    """Temporarily set HOME to a temp directory and patch Path.home()."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    yield tmp_path


@pytest.fixture
def temp_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[Path, None, None]:
    """Temporarily set XDG_CONFIG_HOME to a temp directory."""
    xdg_path = tmp_path / ".config"
    xdg_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_path))
    yield xdg_path


@pytest.fixture
def isolated_registry(temp_xdg: Path) -> Generator[Path, None, None]:
    """Provide an isolated registry path and ensure it's clean."""
    reg = registry_path()
    if reg.exists():
        reg.unlink()
    yield reg


# ---------------------------------------------------------------------------
# registry_path
# ---------------------------------------------------------------------------


class TestRegistryPath:
    def test_uses_xdg_when_set(self, temp_xdg: Path) -> None:
        p = registry_path()
        assert str(temp_xdg) in str(p)
        assert p.name == "confy.yaml"

    def test_falls_back_to_home_config(self, temp_home: Path) -> None:
        p = registry_path()
        expected = temp_home / ".config" / "confy" / "confy.yaml"
        assert p == expected


# ---------------------------------------------------------------------------
# _read_yaml / _write_yaml
# ---------------------------------------------------------------------------


class TestYamlIO:
    def test_write_and_read(self, tmp_path: Path) -> None:
        p = tmp_path / "test.yaml"
        data = {"paths": {"fish": "/home/user/.config/fish/config.fish"}}
        assert _write_yaml(p, data) is True
        assert p.exists()
        result = _read_yaml(p)
        assert result == data

    def test_read_nonexistent(self, tmp_path: Path) -> None:
        p = tmp_path / "nonexistent.yaml"
        assert _read_yaml(p) is None

    def test_read_invalid_yaml(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.yaml"
        p.write_text("{invalid: yaml: broken}")
        result = _read_yaml(p)
        assert result is None

    def test_write_creates_parent_dirs(self, tmp_path: Path) -> None:
        p = tmp_path / "a" / "b" / "c" / "test.yaml"
        assert _write_yaml(p, {"key": "value"}) is True
        assert p.exists()

    def test_write_secure_permissions(self, tmp_path: Path) -> None:
        p = tmp_path / "secure.yaml"
        assert _write_yaml(p, {"key": "value"}) is True
        mode = stat.S_IMODE(p.stat().st_mode)
        assert mode == 0o600


# ---------------------------------------------------------------------------
# load_registry / save_registry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_load_empty_when_no_file(self, isolated_registry: Path) -> None:
        reg = load_registry()
        assert reg == {
            "paths": {}, "config": {}, "defaults": {},
            "hosts": {},
        }

    def test_save_and_load(self, isolated_registry: Path) -> None:
        data = {
            "paths": {"fish": "/home/user/.config/fish/config.fish"},
            "config": {"editor": "vim"},
            "defaults": {"nvim": ["/home/user/.config/nvim/init.lua"]},
        }
        assert save_registry(data) is True
        loaded = load_registry()
        assert loaded["paths"] == {"fish": "/home/user/.config/fish/config.fish"}
        assert loaded["config"] == {"editor": "vim"}
        assert loaded["defaults"] == {"nvim": ["/home/user/.config/nvim/init.lua"]}

    def test_save_cleans_empty_sections(self, isolated_registry: Path) -> None:
        data = {"paths": {}, "config": {}, "defaults": {}}
        assert save_registry(data) is True
        loaded = load_registry()
        assert loaded == {
            "paths": {}, "config": {}, "defaults": {},
            "hosts": {},
        }

    def test_load_with_non_dict_data(self, isolated_registry: Path) -> None:
        _write_yaml(isolated_registry, "just a string")
        reg = load_registry()
        assert reg == {
            "paths": {}, "config": {}, "defaults": {},
            "hosts": {},
        }

    def test_load_with_partial_data(self, isolated_registry: Path) -> None:
        _write_yaml(isolated_registry, {"paths": {"tmux": "/home/user/.tmux.conf"}})
        reg = load_registry()
        assert reg["paths"] == {"tmux": "/home/user/.tmux.conf"}
        assert reg["config"] == {}
        assert reg["defaults"] == {}


# ---------------------------------------------------------------------------
# candidate_paths
# ---------------------------------------------------------------------------


class TestCandidatePaths:
    def test_builtin_defaults(self, temp_home: Path) -> None:
        candidates = candidate_paths("fish")
        assert any("config.fish" in str(c) for c in candidates)

    def test_fallback_patterns(self, temp_home: Path) -> None:
        candidates = candidate_paths("myapp")
        # Should include fallback patterns
        paths_str = [str(c) for c in candidates]
        assert str(temp_home / ".myapprc") in paths_str
        assert str(temp_home / ".myapp_config") in paths_str
        assert str(temp_home / ".config/myapp/config") in paths_str

    def test_user_defaults_from_registry(
        self, isolated_registry: Path, temp_home: Path
    ) -> None:
        save_registry(
            {
                "defaults": {
                    "myapp": [str(temp_home / "custom" / "myapp.conf")],
                }
            }
        )
        candidates = candidate_paths("myapp")
        assert any(str(temp_home / "custom" / "myapp.conf") in str(c) for c in candidates)

    def test_deduplication(self, temp_home: Path) -> None:
        candidates = candidate_paths("fish")
        paths_str = [str(c) for c in candidates]
        assert len(paths_str) == len(set(paths_str))


# ---------------------------------------------------------------------------
# _expand_candidate_to_files
# ---------------------------------------------------------------------------


class TestExpandCandidate:
    def test_returns_file_as_is(self, tmp_path: Path) -> None:
        f = tmp_path / "config.fish"
        f.write_text("")
        result = _expand_candidate_to_files(f, "fish")
        assert result == [f]

    def test_expands_directory(self, tmp_path: Path) -> None:
        d = tmp_path / "nvim"
        d.mkdir()
        init_lua = d / "init.lua"
        init_lua.write_text("")
        result = _expand_candidate_to_files(d, "nvim")
        assert init_lua in result

    def test_returns_nonexistent_path_as_is(self, tmp_path: Path) -> None:
        p = tmp_path / "nonexistent"
        result = _expand_candidate_to_files(p, "test")
        assert result == [p]

    def test_prefers_local_files(self, tmp_path: Path) -> None:
        d = tmp_path / "fish"
        d.mkdir()
        config = d / "config.fish"
        config.write_text("")
        local = d / "config.fish.local"
        local.write_text("")
        result = _expand_candidate_to_files(d, "fish")
        # .local files should come first
        assert result[0] == local
        assert config in result


# ---------------------------------------------------------------------------
# find_existing / find_all_existing
# ---------------------------------------------------------------------------


class TestFindExisting:
    def test_finds_existing_file(self, temp_home: Path) -> None:
        tmux_conf = temp_home / ".tmux.conf"
        tmux_conf.write_text("# tmux config")
        result = find_existing("tmux")
        assert result == tmux_conf

    def test_returns_none_when_not_found(self, temp_home: Path) -> None:
        result = find_existing("nonexistent_tool")
        assert result is None

    def test_find_all_existing(self, temp_home: Path) -> None:
        # Create multiple matching files
        bashrc = temp_home / ".bashrc"
        bashrc.write_text("")
        bash_profile = temp_home / ".bash_profile"
        bash_profile.write_text("")
        results = find_all_existing("bash")
        assert bashrc in results

    def test_find_all_returns_empty_when_none(self, temp_home: Path) -> None:
        results = find_all_existing("nonexistent_tool")
        assert results == []


# ---------------------------------------------------------------------------
# prompt_choose_path
# ---------------------------------------------------------------------------


class TestPromptChoosePath:
    def test_returns_none_for_empty_list(self) -> None:
        assert prompt_choose_path([]) is None

    def test_returns_none_when_not_tty(self) -> None:
        with patch("sys.stdin.isatty", return_value=False):
            result = prompt_choose_path([Path("/a"), Path("/b")])
            assert result is None

    def test_returns_selected_path(self) -> None:
        paths = [Path("/first/path"), Path("/second/path")]
        with (
            patch("sys.stdin.isatty", return_value=True),
            patch("builtins.input", return_value="2"),
        ):
            result = prompt_choose_path(paths)
            assert result == paths[1]

    def test_returns_none_on_cancel(self) -> None:
        paths = [Path("/first/path"), Path("/second/path")]
        with (
            patch("sys.stdin.isatty", return_value=True),
            patch("builtins.input", return_value="q"),
        ):
            result = prompt_choose_path(paths)
            assert result is None


# ---------------------------------------------------------------------------
# choose_editor
# ---------------------------------------------------------------------------


class TestChooseEditor:
    def test_cli_editor_takes_precedence(self) -> None:
        with patch("confy.load_registry", return_value={"config": {}, "paths": {}, "defaults": {}}):
            assert choose_editor("vim") == "vim"

    def test_registry_editor_used(self) -> None:
        registry = {"config": {"editor": "code"}, "paths": {}, "defaults": {}}
        with patch("confy.load_registry", return_value=registry):
            assert choose_editor(None) == "code"

    def test_falls_back_to_visual(self) -> None:
        with (
            patch("confy.load_registry", return_value={"config": {}, "paths": {}, "defaults": {}}),
            patch.dict(os.environ, {"VISUAL": "subl", "EDITOR": ""}, clear=True),
        ):
            assert choose_editor(None) == "subl"

    def test_falls_back_to_editor(self) -> None:
        with (
            patch("confy.load_registry", return_value={"config": {}, "paths": {}, "defaults": {}}),
            patch.dict(os.environ, {"VISUAL": "", "EDITOR": "nano"}, clear=True),
        ):
            assert choose_editor(None) == "nano"

    def test_falls_back_to_nano(self) -> None:
        with (
            patch("confy.load_registry", return_value={"config": {}, "paths": {}, "defaults": {}}),
            patch.dict(os.environ, {"VISUAL": "", "EDITOR": ""}, clear=True),
        ):
            assert choose_editor(None) == "nano"


# ---------------------------------------------------------------------------
# open_in_editor
# ---------------------------------------------------------------------------


class TestOpenInEditor:
    def test_returns_error_for_nonexistent_path(self, tmp_path: Path) -> None:
        p = tmp_path / "nonexistent"
        rc = open_in_editor("vim", p)
        assert rc == 1

    def test_returns_error_for_missing_editor(self, tmp_path: Path) -> None:
        f = tmp_path / "config"
        f.write_text("")
        rc = open_in_editor("nonexistent_editor_xyz", f)
        assert rc == 2

    def test_opens_file_with_editor(self, tmp_path: Path) -> None:
        f = tmp_path / "config"
        f.write_text("")
        rc = open_in_editor("true", f)
        assert rc == 0

    def test_opens_directory_with_editor(self, tmp_path: Path) -> None:
        rc = open_in_editor("true", tmp_path)
        assert rc == 0


# ---------------------------------------------------------------------------
# print_list
# ---------------------------------------------------------------------------


class TestPrintList:
    def test_prints_empty_state(self, capsys: pytest.CaptureFixture[str]) -> None:
        registry = {"paths": {}, "config": {}, "defaults": {}}
        print_list(registry)
        captured = capsys.readouterr()
        assert "Registered paths:" in captured.out
        assert "(none)" in captured.out

    def test_prints_registered_paths(self, capsys: pytest.CaptureFixture[str]) -> None:
        registry = {
            "paths": {"fish": "/home/user/.config/fish/config.fish"},
            "config": {"editor": "vim"},
            "defaults": {},
        }
        print_list(registry)
        captured = capsys.readouterr()
        assert "fish:" in captured.out
        assert "vim" in captured.out


# ---------------------------------------------------------------------------
# register_target
# ---------------------------------------------------------------------------


class TestRegisterTarget:
    def test_registers_given_path(self, isolated_registry: Path, tmp_path: Path) -> None:
        f = tmp_path / "myconfig"
        f.write_text("")
        registry = {"paths": {}, "config": {}, "defaults": {}}
        register_target(registry, "myapp", str(f))
        assert "myapp" in registry["paths"]
        assert registry["paths"]["myapp"] == str(f)

    def test_refuses_nonexistent_path(self, isolated_registry: Path) -> None:
        registry = {"paths": {}, "config": {}, "defaults": {}}
        register_target(registry, "myapp", "/nonexistent/path")
        assert "myapp" not in registry["paths"]

    def test_refuses_root_owned_path(self, isolated_registry: Path, tmp_path: Path) -> None:
        f = tmp_path / "root_owned"
        f.write_text("")
        # Simulate root ownership by patching stat
        original_stat = f.stat

        def mock_stat() -> os.stat_result:
            st = original_stat()
            # Return a modified stat result with uid 0 (root)
            return os.stat_result(
                (
                    st.st_mode,
                    st.st_ino,
                    st.st_dev,
                    st.st_nlink,
                    0,  # st_uid = root
                    st.st_gid,
                    st.st_size,
                    st.st_atime,
                    st.st_mtime,
                    st.st_ctime,
                )
            )

        with patch.object(Path, "stat", return_value=mock_stat()):
            registry = {"paths": {}, "config": {}, "defaults": {}}
            register_target(registry, "myapp", str(f))
            assert "myapp" not in registry["paths"]


# ---------------------------------------------------------------------------
# main() integration tests
# ---------------------------------------------------------------------------


class TestMain:
    def test_help(self) -> None:
        with pytest.raises(SystemExit) as exc:
            main(["--help"])
        assert exc.value.code == 0

    def test_no_target_shows_help(self) -> None:
        rc = main([])
        assert rc == 1

    def test_refuses_root(self) -> None:
        with patch("os.geteuid", return_value=0):
            rc = main(["fish"])
            assert rc == 1

    def test_reset_registry(self, isolated_registry: Path) -> None:
        # Create a registry file first
        save_registry({"paths": {"fish": "/some/path"}})
        assert isolated_registry.exists()
        rc = main(["--reset"])
        assert rc == 0
        assert not isolated_registry.exists()

    def test_reset_when_no_registry(self, isolated_registry: Path) -> None:
        rc = main(["--reset"])
        assert rc == 0

    def test_list(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = main(["--list"])
        assert rc == 0
        captured = capsys.readouterr()
        assert "Registered paths:" in captured.out

    def test_register(self, isolated_registry: Path, tmp_path: Path) -> None:
        f = tmp_path / "myconfig"
        f.write_text("")
        rc = main(["--register", "myapp", str(f)])
        assert rc == 0
        reg = load_registry()
        assert "myapp" in reg["paths"]

    def test_register_requires_target(self) -> None:
        rc = main(["--register"])
        assert rc == 2

    def test_print_dir(self, temp_home: Path, isolated_registry: Path) -> None:
        tmux_conf = temp_home / ".tmux.conf"
        tmux_conf.write_text("")
        rc = main(["--print-dir", "tmux"])
        assert rc == 0

    def test_opens_existing_config(self, temp_home: Path, isolated_registry: Path) -> None:
        tmux_conf = temp_home / ".tmux.conf"
        tmux_conf.write_text("")
        rc = main(["tmux", "--editor", "true"])
        assert rc == 0

    def test_error_for_missing_config(self, temp_home: Path) -> None:
        rc = main(["nonexistent_tool"])
        assert rc == 2

    def test_force_flag_selects_first(self, temp_home: Path) -> None:
        # Create multiple matching files
        bashrc = temp_home / ".bashrc"
        bashrc.write_text("")
        rc = main(["bash", "--editor", "true", "--force"])
        assert rc == 0

    def test_register_with_auto_detect(self, isolated_registry: Path, tmp_path: Path) -> None:
        # Create a file that matches a candidate pattern
        f = tmp_path / ".tmux.conf"
        f.write_text("")
        old_cwd = Path.cwd()
        try:
            os.chdir(str(tmp_path))
            rc = main(["--register", "tmux"])
            assert rc == 0
            reg = load_registry()
            assert "tmux" in reg["paths"]
        finally:
            os.chdir(str(old_cwd))


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_candidate_paths_strips_whitespace(self, temp_home: Path) -> None:
        candidates = candidate_paths("  fish  ")
        assert any("config.fish" in str(c) for c in candidates)

    def test_find_existing_with_directory(self, temp_home: Path) -> None:
        # Create a directory where a config file would be
        nvim_dir = temp_home / ".config" / "nvim"
        nvim_dir.mkdir(parents=True)
        init_lua = nvim_dir / "init.lua"
        init_lua.write_text("")
        result = find_existing("nvim")
        assert result == init_lua

    def test_load_registry_with_malformed_data(self, isolated_registry: Path) -> None:
        isolated_registry.parent.mkdir(parents=True, exist_ok=True)
        isolated_registry.write_text("{malformed: yaml: broken}")
        reg = load_registry()
        assert reg == {
            "paths": {}, "config": {}, "defaults": {},
            "hosts": {},
        }

    def test_save_registry_handles_write_error(self, isolated_registry: Path) -> None:
        # Make the parent directory non-writable
        isolated_registry.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(isolated_registry.parent, 0o444)
        try:
            result = save_registry({"paths": {"test": "/path"}})
            assert result is False
        finally:
            os.chmod(isolated_registry.parent, 0o755)

    def test_open_in_editor_refuses_nonexistent(self, tmp_path: Path) -> None:
        p = tmp_path / "nonexistent"
        rc = open_in_editor("true", p)
        assert rc == 1

    def test_choose_editor_empty_env(self) -> None:
        with (
            patch("confy.load_registry", return_value={"config": {}, "paths": {}, "defaults": {}}),
            patch.dict(os.environ, clear=True),
        ):
            assert choose_editor(None) == "nano"

