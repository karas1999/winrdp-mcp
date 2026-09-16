from __future__ import annotations

from winrdp_mcp.elevation import _interactive_session_from_qwinsta, run_in_user_session
from winrdp_mcp.transports import ExecResult


QWINSTA = """ SESSIONNAME       USERNAME                 ID  STATE   TYPE        DEVICE
 services                                    0  Disc
>console           CloudUser                  1  Active
 rdp-tcp                                 65536  Listen
"""


def test_qwinsta_parser_keeps_session_id_out_of_username():
    assert _interactive_session_from_qwinsta(QWINSTA) == ("CloudUser", 1, "Active")


class _InteractiveTransport:
    def __init__(self):
        self.register_script = ""
        self.uploads = []
        self.calls = []

    def run_ps(self, script, timeout=120):
        self.calls.append(script)
        if script == "qwinsta 2>$null":
            return ExecResult(QWINSTA, "", 0)
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
