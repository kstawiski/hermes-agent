"""Approval behavior for ``rm`` confined to disposable Hermes temp roots."""

from pathlib import Path
from unittest.mock import Mock

import pytest

import tools.approval as approval
from hermes_constants import reset_hermes_home_override, set_hermes_home_override


@pytest.fixture
def temp_roots(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    system_root = tmp_path / "system-tmp"
    system_root.mkdir()
    hermes_root = tmp_path / "hermes-home" / "tmp"
    hermes_root.mkdir(parents=True)
    monkeypatch.setattr(approval.tempfile, "gettempdir", lambda: str(system_root))
    monkeypatch.setenv("HERMES_HOME", str(hermes_root.parent))
    # Keep bare-rm cases independent of the developer or CI runner's PATH.
    monkeypatch.setenv("PATH", "/bin:/usr/bin")
    return system_root, hermes_root


@pytest.fixture
def manual_guards(monkeypatch) -> Mock:
    scanner = Mock(return_value={"action": "allow", "findings": [], "summary": ""})
    monkeypatch.setattr("tools.tirith_security.check_command_security", scanner)
    monkeypatch.setattr(approval, "_get_approval_mode", lambda: "manual")
    monkeypatch.setattr(approval, "_is_interactive_cli", lambda: True)
    monkeypatch.setattr(approval, "_is_gateway_approval_context", lambda: False)
    monkeypatch.setattr(approval, "is_current_session_yolo_enabled", lambda: False)
    monkeypatch.setattr(approval, "_command_matches_permanent_allowlist", lambda _command: False)
    monkeypatch.setattr(approval, "_match_user_deny_rule", lambda _command: None)
    monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", False)
    monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)
    return scanner


def _guard_with_denied_prompt(command: str) -> tuple[dict, Mock]:
    callback = Mock(return_value="deny")
    result = approval.check_all_command_guards(
        command,
        "local",
        approval_callback=callback,
    )
    return result, callback


@pytest.mark.parametrize("env_type", ["ssh", "docker", "modal"])
def test_temp_rm_exemption_is_local_only(temp_roots, manual_guards, env_type, monkeypatch):
    system_root, _ = temp_roots
    monkeypatch.setenv("HERMES_EXEC_ASK", "1")
    temp_check = Mock(side_effect=AssertionError("non-local backend evaluated local temp exemption"))
    monkeypatch.setattr(approval, "_is_confined_temp_tree_cleanup", temp_check)
    callback = Mock(return_value="deny")
    result = approval.check_all_command_guards(
        f"rm -rf {system_root / 'remote-tree'}",
        env_type,
        approval_callback=callback,
    )
    assert isinstance(result["approved"], bool)
    temp_check.assert_not_called()


@pytest.mark.parametrize(
    "command_factory",
    [
        lambda system, _hermes: f"rm -rf {system / 'tree'}",
        lambda _system, hermes: (
            f"rm --recursive --force {hermes / 'first'} {hermes / 'second'}"
        ),
        lambda system, _hermes: f"/bin/rm -r -f -- {system / 'tree with space'!s}",
    ],
)
def test_confined_temp_rm_bypasses_scanner_and_prompt(
    temp_roots,
    manual_guards,
    command_factory,
):
    command = command_factory(*temp_roots)
    if "tree with space" in command:
        command = command.replace("tree with space", "'tree with space'")

    result, callback = _guard_with_denied_prompt(command)

    assert result == {"approved": True, "message": None}
    manual_guards.assert_not_called()
    callback.assert_not_called()


def test_unsafe_rm_variants_still_require_approval(temp_roots, manual_guards):
    system_root, hermes_root = temp_roots
    traversal = system_root / "nested" / ".." / "victim"
    cases = [
        f"rm -rf {system_root}",
        f"rm -rf {hermes_root}",
        f"rm -rf {traversal}",
        "rm -rf relative/path",
        f"rm -rf {system_root / 'tree'} && true",
        f"rm -rf {system_root / 'tree'}; true",
        f"rm -rf {system_root / 'tree'} | true",
        f"rm -rf {system_root / 'tree'} > /dev/null",
        f"rm -rf $(printf %s {system_root / 'tree'})",
        f"rm -rf {system_root / 'tree'}\\ with-space",
        f"rm -rf {system_root / '*'}",
        f"rm -rf {system_root / 'tree'} {system_root.parent / 'outside'}",
        f"rm --unknown -rf {system_root / 'tree'}",
    ]

    for command in cases:
        result, callback = _guard_with_denied_prompt(command)
        assert result["approved"] is False, command
        callback.assert_called_once()

    assert manual_guards.call_count == len(cases)


def test_operand_symlink_escape_still_requires_approval(temp_roots, manual_guards, tmp_path):
    system_root, _ = temp_roots
    outside = tmp_path / "outside"
    outside.mkdir()
    link = system_root / "link"
    link.symlink_to(outside, target_is_directory=True)

    result, callback = _guard_with_denied_prompt(f"rm -rf {link / 'victim'}")

    assert result["approved"] is False
    manual_guards.assert_called_once()
    callback.assert_called_once()


def test_rebound_hermes_temp_root_still_requires_approval(
    temp_roots,
    manual_guards,
    tmp_path,
):
    _, hermes_root = temp_roots
    outside = tmp_path / "outside-root"
    outside.mkdir()
    hermes_root.rmdir()
    hermes_root.symlink_to(outside, target_is_directory=True)

    result, callback = _guard_with_denied_prompt(f"rm -rf {hermes_root / 'victim'}")

    assert result["approved"] is False
    manual_guards.assert_called_once()
    callback.assert_called_once()


def test_context_local_hermes_home_override_controls_temp_root(
    temp_roots,
    manual_guards,
    tmp_path,
):
    _, process_hermes_tmp = temp_roots
    profile_home = tmp_path / "profile-home"
    profile_tmp = profile_home / "tmp"
    profile_tmp.mkdir(parents=True)
    token = set_hermes_home_override(profile_home)
    try:
        approved, approved_callback = _guard_with_denied_prompt(
            f"rm -rf {profile_tmp / 'victim'}"
        )
        denied, denied_callback = _guard_with_denied_prompt(
            f"rm -rf {process_hermes_tmp / 'victim'}"
        )
    finally:
        reset_hermes_home_override(token)

    assert approved == {"approved": True, "message": None}
    approved_callback.assert_not_called()
    assert denied["approved"] is False
    denied_callback.assert_called_once()
    assert manual_guards.call_count == 1


def test_confined_temp_rm_bypasses_cron_deny(temp_roots, monkeypatch):
    system_root, _ = temp_roots
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
    monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)
    monkeypatch.setattr(approval, "_get_approval_mode", lambda: "manual")
    monkeypatch.setattr(approval, "is_current_session_yolo_enabled", lambda: False)
    monkeypatch.setattr(approval, "_command_matches_permanent_allowlist", lambda _command: False)
    monkeypatch.setattr(approval, "_match_user_deny_rule", lambda _command: None)
    monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setattr(approval, "_get_cron_approval_mode", lambda: "deny")

    result = approval.check_all_command_guards(
        f"rm -rf {system_root / 'tree'}",
        "local",
    )

    assert result == {"approved": True, "message": None}


def test_user_deny_rule_precedes_temp_cleanup_bypass(
    temp_roots,
    manual_guards,
    monkeypatch,
):
    system_root, _ = temp_roots
    monkeypatch.setattr(approval, "_match_user_deny_rule", lambda _command: "rm *")

    result = approval.check_all_command_guards(f"rm -rf {system_root / 'tree'}", "local")

    assert result["approved"] is False
    assert result["user_deny"] is True
    manual_guards.assert_not_called()


def test_option_looking_token_after_first_operand_requires_approval(
    temp_roots,
    manual_guards,
):
    system_root, _ = temp_roots

    result, callback = _guard_with_denied_prompt(
        f"rm {system_root / 'tree'} --recursive"
    )

    assert result["approved"] is False
    manual_guards.assert_called_once()
    callback.assert_called_once()


def test_bare_rm_shadowed_on_path_requires_approval(
    temp_roots,
    manual_guards,
    monkeypatch,
    tmp_path,
):
    system_root, _ = temp_roots
    shadow_dir = tmp_path / "shadow-bin"
    shadow_dir.mkdir()
    shadow_rm = shadow_dir / "rm"
    shadow_rm.write_text("#!/bin/sh\nexit 0\n")
    shadow_rm.chmod(0o755)
    monkeypatch.setenv("PATH", f"{shadow_dir}{approval.os.pathsep}/bin:/usr/bin")

    result, callback = _guard_with_denied_prompt(f"rm -rf {system_root / 'tree'}")

    assert result["approved"] is False
    manual_guards.assert_called_once()
    callback.assert_called_once()


def test_non_system_rm_executable_path_requires_approval(
    temp_roots,
    manual_guards,
    tmp_path,
):
    system_root, _ = temp_roots
    alternate_rm = tmp_path / "rm"
    alternate_rm.symlink_to("/bin/rm")

    result, callback = _guard_with_denied_prompt(
        f"{alternate_rm} -rf {system_root / 'tree'}"
    )

    assert result["approved"] is False
    manual_guards.assert_called_once()
    callback.assert_called_once()


@pytest.mark.parametrize("executable", ["/bin/rm", "/usr/bin/rm", "rm"])
def test_trusted_rm_executable_forms_bypass_approval(
    executable,
    temp_roots,
    manual_guards,
    monkeypatch,
):
    system_root, _ = temp_roots
    monkeypatch.setenv("PATH", "/bin:/usr/bin")

    result, callback = _guard_with_denied_prompt(
        f"{executable} -rf {system_root / 'tree'}"
    )

    assert result == {"approved": True, "message": None}
    manual_guards.assert_not_called()
    callback.assert_not_called()


@pytest.mark.parametrize("codepoint", [*range(32), 127])
def test_ascii_control_characters_require_approval(
    codepoint,
    temp_roots,
    manual_guards,
):
    system_root, _ = temp_roots
    command = f"rm -rf {system_root / 'tree'}{chr(codepoint)}"

    result, callback = _guard_with_denied_prompt(command)

    assert result["approved"] is False
    manual_guards.assert_called_once()
    callback.assert_called_once()


@pytest.mark.parametrize("path_function", ["realpath", "commonpath"])
def test_path_resolution_errors_fail_closed_with_guard_result(
    path_function,
    temp_roots,
    manual_guards,
    monkeypatch,
):
    system_root, _ = temp_roots

    def raise_os_error(*_args, **_kwargs):
        raise OSError("simulated path resolution failure")

    monkeypatch.setattr(approval.os.path, path_function, raise_os_error)

    result, callback = _guard_with_denied_prompt(f"rm -rf {system_root / 'tree'}")

    assert isinstance(result, dict)
    assert result["approved"] is False
    manual_guards.assert_called_once()
    callback.assert_called_once()


def test_filesystem_root_cannot_be_system_temp_root(
    temp_roots,
    manual_guards,
    monkeypatch,
):
    monkeypatch.setattr(approval.tempfile, "gettempdir", lambda: approval.os.sep)

    result, callback = _guard_with_denied_prompt("rm -rf /var/tmp/hermes-victim")

    assert result["approved"] is False
    manual_guards.assert_called_once()
    callback.assert_called_once()