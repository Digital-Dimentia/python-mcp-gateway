"""Path resolution, and the `--check` / `--list` reports that must never print a value."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from mcp_gateway import cli

SECRET = "ghp_supersecretvalue"


def _write(tmp_path: Path, *, servers: str, env: str = "") -> tuple[Path, Path]:
    config_path = tmp_path / "servers.yaml"
    config_path.write_text(servers)
    env_path = tmp_path / "gateway.env"
    env_path.write_text(env)
    return config_path, env_path


def test_env_defaults_beside_the_config_file_not_the_cwd(tmp_path) -> None:
    """launchd starts a job in `/`. Resolving against the cwd would fail exactly there."""
    config_path = tmp_path / "nested" / "servers.yaml"
    config_path.parent.mkdir()
    assert cli.default_env_path(config_path, environ={}) == config_path.parent / "gateway.env"


def test_explicit_environment_overrides_win(tmp_path) -> None:
    environ = {cli.CONFIG_ENV: "/etc/servers.yaml", cli.ENV_FILE_ENV: "/run/secrets/env"}
    assert cli.default_config_path(environ) == Path("/etc/servers.yaml")
    assert cli.default_env_path(Path("/etc/servers.yaml"), environ) == Path("/run/secrets/env")


def test_check_passes_when_every_enabled_server_has_its_credentials(tmp_path, capsys) -> None:
    config_path, env_path = _write(
        tmp_path,
        servers='servers:\n  a:\n    command: x\n    env:\n      K: "${TOKEN}"\n',
        env=f"TOKEN={SECRET}\n",
    )
    assert cli.check(config_path, env_path) == 0
    assert "all enabled servers have their credentials" in capsys.readouterr().err


def test_check_warns_but_passes_when_an_optional_server_is_unconfigured(tmp_path, capsys) -> None:
    """One expired token must not cost the operator every other tool."""
    config_path, env_path = _write(
        tmp_path, servers='servers:\n  a:\n    command: x\n    env:\n      K: "${GONE}"\n'
    )
    assert cli.check(config_path, env_path) == 0
    err = capsys.readouterr().err
    assert "WARN" in err and "GONE" in err
    assert "would be skipped" in err


def test_check_fails_when_a_required_server_is_unconfigured(tmp_path, capsys) -> None:
    config_path, env_path = _write(
        tmp_path,
        servers='servers:\n  a:\n    command: x\n    required: true\n    env:\n      K: "${GONE}"\n',
    )
    assert cli.check(config_path, env_path) == cli.EXIT_REFUSED
    assert "ERROR" in capsys.readouterr().err


def test_check_ignores_a_disabled_servers_missing_credential(tmp_path, capsys) -> None:
    """Parking a backend is how an operator defers dealing with its credential."""
    config_path, env_path = _write(
        tmp_path,
        servers=(
            'servers:\n  a:\n    command: x\n    enabled: false\n'
            '    required: true\n    env:\n      K: "${GONE}"\n'
        ),
    )
    assert cli.check(config_path, env_path) == 0
    assert "GONE" not in capsys.readouterr().err


def test_check_reports_every_problem_not_just_the_first(tmp_path, capsys) -> None:
    config_path, env_path = _write(
        tmp_path,
        servers=(
            'servers:\n  a:\n    command: x\n    env:\n      K: "${ONE}"\n'
            '  b:\n    command: y\n    env:\n      K: "${TWO}"\n'
        ),
    )
    cli.check(config_path, env_path)
    err = capsys.readouterr().err
    assert "ONE" in err and "TWO" in err


def test_list_prints_env_key_names_and_never_values(tmp_path, capsys) -> None:
    """`--list` exists so an operator can check the plan without opening the store."""
    config_path, env_path = _write(
        tmp_path,
        servers=(
            'servers:\n  a:\n    command: npx\n    args: ["-y", "srv"]\n'
            '    env:\n      MY_TOKEN: "${TOKEN}"\n'
        ),
        env=f"TOKEN={SECRET}\n",
    )
    cli.check(config_path, env_path, list_plan=True)
    err = capsys.readouterr().err
    assert "MY_TOKEN" in err
    assert SECRET not in err
    assert "npx -y srv" in err


def test_check_refuses_a_broken_config_with_exit_two(tmp_path, capsys) -> None:
    config_path, env_path = _write(tmp_path, servers="servers:\n  a:\n    typo: 1\n")
    assert cli.check(config_path, env_path) == cli.EXIT_REFUSED
    assert "unknown key" in capsys.readouterr().err


def test_check_refuses_a_broken_credential_store_with_exit_two(tmp_path, capsys) -> None:
    config_path, env_path = _write(
        tmp_path, servers="servers:\n  a:\n    command: x\n", env="TOKEN=a\nTOKEN=b\n"
    )
    assert cli.check(config_path, env_path) == cli.EXIT_REFUSED
    assert "duplicate" in capsys.readouterr().err


def test_the_parser_advertises_the_defaults_the_makefile_banner_quotes() -> None:
    args = cli.build_parser().parse_args([])
    assert (args.host, args.port) == (cli.DEFAULT_HOST, cli.DEFAULT_PORT)
    assert args.config is None and args.env is None
    assert not args.check and not args.list_plan and not args.debug


def test_the_module_is_runnable_as_a_script(tmp_path) -> None:
    """`python -m mcp_gateway.cli` must actually run, because that is how the daemon starts.

    `scripts/start-gateway.sh` -- and therefore `make run`, `make run-dev` and the launchd
    plist -- invokes the module, not the console script. Without an `if __name__` guard the
    module imports, defines everything, and exits 0 without binding anything, which looks
    exactly like a daemon that started: the Makefile prints the banner either way. Nothing
    caught it, because every test and every hand-run used the `mcp-gateway` entry point.

    `--check` rather than a real bind: it exercises the same `run()` and exits.
    """
    config_path, env_path = _write(
        tmp_path, servers="servers:\n  alpha:\n    command: /usr/bin/true\n"
    )
    result = subprocess.run(
        [sys.executable, "-m", "mcp_gateway.cli", "--check",
         "--config", str(config_path), "--env", str(env_path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    # Proof it got as far as `check()` rather than falling off the end of an import.
    assert "1 enabled server(s)" in result.stdout + result.stderr
