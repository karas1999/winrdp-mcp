from __future__ import annotations

from winrdp_mcp.elevation import (
    _interactive_session_from_console_probe,
    _interactive_session_from_qwinsta,
    run_in_user_session,
)
from winrdp_mcp.transports import ExecResult


QWINSTA = """ SESSIONNAME       USERNAME                 ID  STATE   TYPE        DEVICE
 services                                    0  Disc
>console           CloudUser                  1  Active
 rdp-tcp                                 65536  Listen
"""


def test_qwinsta_parser_keeps_session_id_out_of_username():
    assert _interactive_session_from_qwinsta(QWINSTA) == ("CloudUser", 1, "Active")


def test_console_fallback_parser_returns_active_session():
    assert _interactive_session_from_console_probe(
        "noise\nACTIVE:1:KARASLAPTOP\\karas\n"
    ) == ("KARASLAPTOP\\karas", 1, "Active")


class _InteractiveTransport:
    def __init__(self, *, qwinsta: str = QWINSTA):
        self.register_script = ""
        self.uploads = []
        self.calls = []
        self.qwinsta = qwinsta

    def run_ps(self, script, timeout=120):
        self.calls.append(script)
        if script == "qwinsta 2>$null":
            return ExecResult(self.qwinsta, "", 0 if self.qwinsta else 1)
        if "Win32_ComputerSystem" in script and "explorer.exe" in script:
            return ExecResult("ACTIVE:1:KARASLAPTOP\\karas\n", "", 0)
        if "GetOwnerSid" in script:
            return ExecResult("S-1-12-1-111-222-333-444\n", "", 0)
        if "Register-ScheduledTask" in script:
            self.register_script = script
            return ExecResult("", "", 0)
        if script.startswith("Test-Path -LiteralPath"):
            return ExecResult("True\n", "", 0)
        if "Get-Content -LiteralPath" in script and ".done" in script:
            return ExecResult("rc=0\n", "", 0)
        if "Get-Content -LiteralPath" in script and ".out" in script:
            return ExecResult("ok\n", "", 0)
        return ExecResult("", "", 0)

    def upload(self, data, remote_path, timeout=300):
        self.uploads.append((data, remote_path))


def test_interactive_task_uses_shell_owner_sid_when_available():
    transport = _InteractiveTransport()
    result = run_in_user_session(transport, "'ok'", timeout=1)

    assert result.rc == 0
    assert "-UserId 'S-1-12-1-111-222-333-444'" in transport.register_script
    assert "-WindowStyle Hidden" in transport.register_script
    sid_probe = next(script for script in transport.calls if "GetOwnerSid" in script)
    assert "$id=1;" in sid_probe


def test_interactive_task_falls_back_when_qwinsta_is_unavailable():
    transport = _InteractiveTransport(qwinsta="")
    result = run_in_user_session(transport, "'ok'", timeout=1)

    assert result.rc == 0
    fallback = next(script for script in transport.calls if "Win32_ComputerSystem" in script)
    assert "explorer.exe" in fallback
    assert "-UserId 'S-1-12-1-111-222-333-444'" in transport.register_script
