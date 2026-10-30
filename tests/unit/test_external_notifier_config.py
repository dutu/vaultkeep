"""External notification schema and conservative literal hook dependency checks."""

from __future__ import annotations

from copy import deepcopy

import pytest

from vaultkeep.config.hook_commands import invokes_notify
from vaultkeep.config.loader import validate_document
from vaultkeep.errors import ConfigurationError


@pytest.mark.parametrize(
    "command",
    [
        'vaultkeep notify --level info "done"',
        '/usr/local/bin/vaultkeep notify "done"',
        '"/opt/current/bin/vaultkeep" --user --config "/tmp/job file.yaml" notify x',
        "vaultkeep --user --no-color --config=/job.yaml --param A=b notify x",
        "vaultkeep --config /job.yaml --param A=b notify x",
        "true; vaultkeep notify x",
        "false || vaultkeep notify x",
        "printf x | vaultkeep notify",
        "true && vaultkeep notify x &",
        "true;\nvaultkeep notify x",
        "vaultkeep \\\nnotify x",
        "true\nvaultkeep notify x",
        "echo x # comment\nvaultkeep notify x",
        "(vaultkeep notify x)",
        "{ vaultkeep notify x; }",
        "if true; then vaultkeep notify x; fi",
        "A=value vaultkeep notify x",
    ],
)
def test_literal_invocations_are_detected(command):
    assert invokes_notify(command)


@pytest.mark.parametrize(
    "command",
    [
        'echo "vaultkeep notify"',
        'printf "%s" "vaultkeep" "notify"',
        'printf "%s" "text; vaultkeep notify x"',
        "printf '%s' ';' vaultkeep notify",
        r"printf '%s' \; vaultkeep notify",
        r"printf '%s' \| vaultkeep notify",
        'echo "\nvaultkeep notify x"',
        "other-vaultkeep notify x",
        "/bin/vaultkeep-other notify x",
        "echo vaultkeep notify x",
        'MESSAGE="text; vaultkeep notify secret"; printf "%s" "$MESSAGE"',
        "MESSAGE='text; vaultkeep notify secret'; echo x",
        "vaultkeep status",
        "vaultkeep notify-telegram x",
        "vaultkeep --help notify",
        "vaultkeep --version notify",
        "# vaultkeep notify x\ntrue",
        "sh -c 'vaultkeep notify x'",
        "/path/to/script.sh",
        "$COMMAND notify x",
        "cat <<'EOF'\nvaultkeep notify x\nEOF",
        'echo "unterminated',
    ],
)
def test_text_and_indirect_or_unrelated_commands_are_not_invocations(command):
    assert not invokes_notify(command)


@pytest.mark.parametrize(
    "phase",
    [
        "before_check",
        "before_archive",
        "after_archive",
        "on_success",
        "on_failure",
        "on_unchanged",
    ],
)
def test_direct_hook_requires_notifier_with_field_specific_error(valid_config, phase):
    valid_config["hooks"][phase] = {"command": 'vaultkeep notify "done"'}
    with pytest.raises(ConfigurationError) as caught:
        validate_document(valid_config)
    assert len(caught.value.issues) == 1
    issue = caught.value.issues[0]
    assert issue.dotted_path == f"hooks.{phase}.command"
    assert issue.message == "Uses 'vaultkeep notify', but external_notifier is not configured."


@pytest.mark.parametrize("configured,used", [(False, False), (True, False), (True, True)])
def test_notifier_is_optional_unless_a_supported_hook_uses_it(valid_config, configured, used):
    if configured:
        valid_config["external_notifier"] = {"command": "not-installed-notifier"}
    if used:
        valid_config["hooks"]["on_success"] = {"command": 'vaultkeep notify "done"'}
    config = validate_document(valid_config)
    if configured:
        assert config.external_notifier.timeout_seconds == 15
    else:
        assert config.external_notifier is None


@pytest.mark.parametrize(
    "settings,field",
    [
        ({}, "command"),
        ({"command": ""}, "command"),
        ({"command": "tool --option"}, "command"),
        ({"command": "/bin/tool --option"}, "command"),
        ({"command": "a | b"}, "command"),
        ({"command": "a&&b"}, "command"),
        ({"command": "$(tool)"}, "command"),
        ({"command": "./tool"}, "command"),
        ({"command": "tool\0"}, "command"),
        ({"command": "tool\nother"}, "command"),
        ({"command": ["tool"]}, "command"),
        ({"command": "tool", "args": []}, "args"),
        *[
            ({"command": "tool", "timeout_seconds": value}, "timeout_seconds")
            for value in [0, -1, 3601, float("nan"), float("inf"), "15", True]
        ],
    ],
)
def test_notifier_rejects_invalid_fields(valid_config, settings, field):
    valid_config["external_notifier"] = settings
    with pytest.raises(ConfigurationError) as caught:
        validate_document(valid_config)
    assert f"external_notifier.{field}" in {issue.dotted_path for issue in caught.value.issues}


@pytest.mark.parametrize(
    "command", ["telegram-notify", "custom_notifier", "/opt/bin/custom-notifier"]
)
def test_executable_names_and_absolute_paths_are_allowed_without_installation(
    valid_config, command
):
    valid_config["external_notifier"] = {"command": command, "timeout_seconds": 0.5}
    assert validate_document(valid_config).external_notifier.command == command


def test_legacy_notifications_key_is_rejected_with_rename_hint(valid_config):
    valid_config["notifications"] = {"backup": {"enabled": True}}
    with pytest.raises(ConfigurationError) as caught:
        validate_document(valid_config)
    assert caught.value.issues[0].dotted_path == "notifications"
    assert "Rename notifications to desktop_notifications" in str(caught.value)


def test_desktop_defaults_and_switches_are_preserved(valid_config):
    defaults = validate_document(valid_config).desktop_notifications
    assert defaults.command == "notify-send" and defaults.timeout_ms == 5000
    assert not defaults.backup.enabled and not defaults.trigger.enabled
    document = deepcopy(valid_config)
    document["desktop_notifications"] = {"backup": {"enabled": True}, "trigger": {"enabled": True}}
    enabled = validate_document(document).desktop_notifications
    assert enabled.backup.enabled and enabled.trigger.enabled
