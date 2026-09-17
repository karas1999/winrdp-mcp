from __future__ import annotations

from winrdp_mcp.elevation import run_in_user_session
from winrdp_mcp.transports import ExecResult


class _HomeInteractiveTransport:
    def __init__(self):
        self.register_script = ""
        self.uploads = []
        self.calls = []

    def run_ps(self, script, timeout=120):
        self.calls.append(script)
        if "Get-Command qwinsta" in script:
            return ExecResult("ACTIVE:KARASLAPTOP\\karas\n", "", 0)
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


def test_interactive_task_has_windows_home_fallback():
    transport = _HomeInteractiveTransport()
    result = run_in_user_session(transport, "'ok'", timeout=1)

    assert result.rc == 0
    probe = next(script for script in transport.calls if "Get-Command qwinsta" in script)
    assert "Win32_ComputerSystem" in probe
    assert "explorer.exe" in probe
    assert "-UserId 'KARASLAPTOP\\karas'" in transport.register_script
