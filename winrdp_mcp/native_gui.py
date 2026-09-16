"""Direct Windows desktop helpers for a locally running interactive agent.

This module intentionally avoids PowerShell ``Add-Type``.  When the winrdp agent itself
runs in the logged-on user's Windows session, ordinary GUI work can call Win32/UI
Automation directly from Python.  That avoids CLM restrictions, Scheduled Task latency,
visible helper consoles, and (for the non-invasive operations) moving the user's real
mouse cursor.

Remote hosts still use the existing PowerShell/as_user path; this module is only the
fast/direct path for the local Windows target.
"""

from __future__ import annotations

import ctypes
import io
import os
import threading
import time
from ctypes import wintypes
from typing import Any, Optional


class NativeGuiUnavailable(RuntimeError):
    pass


if os.name == "nt":
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    _user32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
    _user32.EnumWindows.restype = wintypes.BOOL
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.IsWindowVisible.restype = wintypes.BOOL
    _user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    _user32.GetWindowTextLengthW.restype = ctypes.c_int
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetWindowTextW.restype = ctypes.c_int
    _user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    _user32.GetWindowRect.restype = wintypes.BOOL
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    _user32.GetShellWindow.argtypes = []
    _user32.GetShellWindow.restype = wintypes.HWND
    _user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.ShowWindow.restype = wintypes.BOOL
    _user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _user32.SetForegroundWindow.restype = wintypes.BOOL
    _user32.WindowFromPoint.argtypes = [wintypes.POINT]
    _user32.WindowFromPoint.restype = wintypes.HWND
    _user32.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    _user32.ScreenToClient.restype = wintypes.BOOL
    _user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    _user32.PostMessageW.restype = wintypes.BOOL
    _send_message_timeout = getattr(_user32, "SendMessageTimeoutW", None)
    if _send_message_timeout is not None:
        _send_message_timeout.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
            wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t),
        ]
        _send_message_timeout.restype = wintypes.LPARAM
    _user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetClassNameW.restype = ctypes.c_int

    # Keep screenshot pixels, UI Automation rectangles and Win32 screen coordinates in the
    # same physical-pixel space on scaled/multi-monitor desktops.  Probe APIs instead of
    # assuming a particular Windows generation so importing the controller remains safe.
    try:
        set_dpi_context = getattr(_user32, "SetProcessDpiAwarenessContext", None)
        if set_dpi_context is not None:
            set_dpi_context.argtypes = [wintypes.HANDLE]
            set_dpi_context.restype = wintypes.BOOL
            set_dpi_context(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
        else:
            raise AttributeError
    except Exception:
        try:
            set_dpi_aware = getattr(_user32, "SetProcessDPIAware", None)
            if set_dpi_aware is not None:
                set_dpi_aware.argtypes = []
                set_dpi_aware.restype = wintypes.BOOL
                set_dpi_aware()
        except Exception:
            pass

    _kernel32.GetCurrentProcessId.argtypes = []
    _kernel32.GetCurrentProcessId.restype = wintypes.DWORD
    _kernel32.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.ProcessIdToSessionId.restype = wintypes.BOOL
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                     ctypes.POINTER(wintypes.DWORD)]
    _kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL


_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SW_RESTORE = 9

_WM_SETTEXT = 0x000C
_WM_MOUSEMOVE = 0x0200
_WM_LBUTTONDOWN = 0x0201
_WM_LBUTTONUP = 0x0202
_WM_RBUTTONDOWN = 0x0204
_WM_RBUTTONUP = 0x0205
_WM_MBUTTONDOWN = 0x0207
_WM_MBUTTONUP = 0x0208
_MK_LBUTTON = 0x0001
_MK_RBUTTON = 0x0002
_MK_MBUTTON = 0x0010

_BUTTON_MESSAGES = {
    "left": (_WM_LBUTTONDOWN, _WM_LBUTTONUP, _MK_LBUTTON),
    "right": (_WM_RBUTTONDOWN, _WM_RBUTTONUP, _MK_RBUTTON),
    "middle": (_WM_MBUTTONDOWN, _WM_MBUTTONUP, _MK_MBUTTON),
}

_virtual_lock = threading.Lock()
_virtual_pointer: dict[str, Any] = {"x": 0, "y": 0, "hwnd": 0}


def _require_windows() -> None:
    if os.name != "nt":
        raise NativeGuiUnavailable("direct native GUI control is Windows-only")


def _session_id(pid: int) -> Optional[int]:
    _require_windows()
    sid = wintypes.DWORD()
    if not _kernel32.ProcessIdToSessionId(int(pid), ctypes.byref(sid)):
        return None
    return int(sid.value)


def direct_available() -> bool:
    """True when this Python process is running in the same interactive shell session."""
    if os.name != "nt":
        return False
    try:
        shell = _user32.GetShellWindow()
        if not shell:
            return False
        shell_pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(shell, ctypes.byref(shell_pid))
        current_sid = _session_id(int(_kernel32.GetCurrentProcessId()))
        shell_sid = _session_id(int(shell_pid.value))
        return current_sid is not None and current_sid == shell_sid
    except Exception:
        return False


def _window_text(hwnd: int) -> str:
    length = _user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buf, len(buf))
    return buf.value


def _class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    if _user32.GetClassNameW(hwnd, buf, len(buf)):
        return buf.value
    return ""


def _process_name(pid: int) -> str:
    handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        if _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return ""
    finally:
        _kernel32.CloseHandle(handle)


def list_windows() -> list[dict[str, Any]]:
    """Enumerate visible titled top-level windows without PowerShell/UIA dependencies."""
    _require_windows()
    out: list[dict[str, Any]] = []

    @_WNDENUMPROC
    def callback(hwnd, _lparam):
        if not _user32.IsWindowVisible(hwnd):
            return True
        title = _window_text(hwnd)
        if not title:
            return True
        rect = wintypes.RECT()
        if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return True
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        out.append({
            "title": title,
            "process": _process_name(int(pid.value)),
            "pid": int(pid.value),
            "handle": int(hwnd),
            "left": int(rect.left),
            "top": int(rect.top),
            "width": int(rect.right - rect.left),
            "height": int(rect.bottom - rect.top),
        })
        return True

    if not _user32.EnumWindows(callback, 0):
        err = ctypes.get_last_error()
        if err:
            raise OSError(err, "EnumWindows failed")
    return out


def _find_window(title: str) -> Optional[dict[str, Any]]:
    needle = title.casefold()
    windows = list_windows()
    for item in windows:
        if item["title"].casefold() == needle:
            return item
    for item in windows:
        if needle in item["title"].casefold():
            return item
    return None


def focus_window(title: str) -> dict[str, Any]:
    _require_windows()
    item = _find_window(title)
    if not item:
        return {"activated": False, "title": title, "error": "window not found"}
    hwnd = item["handle"]
    _user32.ShowWindow(hwnd, _SW_RESTORE)
    ok = bool(_user32.SetForegroundWindow(hwnd))
    return {"activated": ok, "title": item["title"], "handle": hwnd, "mode": "native"}


def wait_for_window(title: str, timeout: int = 60, interval: float = 1.0) -> dict[str, Any]:
    deadline = time.time() + max(0, timeout)
    while True:
        item = _find_window(title)
        if item:
            return {"reached": True, "window": item, "mode": "native"}
        if time.time() >= deadline:
            return {"reached": False, "mode": "native"}
        time.sleep(max(0.05, interval))


def screenshot_png() -> bytes:
    """Capture the whole virtual desktop using Pillow ImageGrab."""
    _require_windows()
    try:
        from PIL import ImageGrab
    except ImportError as exc:  # pragma: no cover - dependency error is explicit at runtime
        raise NativeGuiUnavailable("Pillow is required for direct screenshot capture") from exc
    image = ImageGrab.grab(all_screens=True)
    try:
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        return buf.getvalue()
    finally:
        image.close()


def _uia_module():
    try:
        import uiautomation as auto
    except ImportError as exc:  # pragma: no cover
        raise NativeGuiUnavailable("uiautomation is required for direct UI Automation") from exc
    return auto


def _control_dict(control, depth: int = 0) -> dict[str, Any]:
    rect = control.BoundingRectangle
    return {
        "name": control.Name or "",
        "type": (control.ControlTypeName or "").removesuffix("Control"),
        "automation_id": control.AutomationId or "",
        "class_name": control.ClassName or "",
        "enabled": bool(control.IsEnabled),
        "pid": int(control.ProcessId or 0),
        "handle": int(control.NativeWindowHandle or 0),
        "depth": int(depth),
        "x": int((rect.left + rect.right) / 2),
        "y": int((rect.top + rect.bottom) / 2),
        "left": int(rect.left),
        "top": int(rect.top),
        "width": int(rect.right - rect.left),
        "height": int(rect.bottom - rect.top),
    }


def _uia_window(window: str):
    auto = _uia_module()
    needle = window.casefold()
    partial = None
    for control in auto.GetRootControl().GetChildren():
        try:
            if control.ControlTypeName != "WindowControl":
                continue
            title = control.Name or ""
        except Exception:
            continue
        if title.casefold() == needle:
            return control
        if partial is None and needle in title.casefold():
            partial = control
    return partial


def ui_find(name: Optional[str] = None, control_type: Optional[str] = None,
            top: int = 60, max_depth: int = 12, window: Optional[str] = None) -> list[dict[str, Any]]:
    """Find controls through Windows UI Automation without moving the real cursor."""
    auto = _uia_module()
    name_needle = name.casefold() if name else None
    type_needle = control_type.casefold().removesuffix("control") if control_type else None
    result: list[dict[str, Any]] = []
    root = _uia_window(window) if window else auto.GetRootControl()
    if root is None:
        return []
    for control, depth in auto.WalkControl(root, includeTop=False, maxDepth=max_depth):
        try:
            control_name = control.Name or ""
            control_type_name = (control.ControlTypeName or "").casefold().removesuffix("control")
            if name_needle and name_needle not in control_name.casefold():
                continue
            if type_needle and control_type_name != type_needle:
                continue
            result.append(_control_dict(control, depth))
            if len(result) >= max(1, int(top)):
                break
        except Exception:
            continue
    return result


def _find_control_by_name(name: str, max_depth: int = 15, window: Optional[str] = None):
    auto = _uia_module()
    needle = name.casefold()
    partial = None
    root = _uia_window(window) if window else auto.GetRootControl()
    if root is None:
        return None
    for control, _depth in auto.WalkControl(root, includeTop=False, maxDepth=max_depth):
        try:
            current = control.Name or ""
        except Exception:
            continue
        if current.casefold() == needle:
            return control
        if partial is None and needle in current.casefold():
            partial = control
    return partial


def ui_invoke(name: str, window: Optional[str] = None) -> dict[str, Any]:
    """Invoke/toggle a UIA control without moving the user's cursor."""
    control = _find_control_by_name(name, window=window)
    if control is None:
        return {"ok": False, "error": "element not found", "mode": "uia"}
    info = _control_dict(control)

    getter = getattr(control, "GetInvokePattern", None)
    if getter:
        try:
            pattern = getter()
            if pattern:
                pattern.Invoke()
                return {"ok": True, "action": "invoke", "mode": "uia", **info}
        except Exception:
            pass

    getter = getattr(control, "GetTogglePattern", None)
    if getter:
        try:
            pattern = getter()
            if pattern:
                pattern.Toggle()
                return {"ok": True, "action": "toggle", "mode": "uia", **info}
        except Exception:
            pass

    return {
        "ok": False,
        "error": "control has no supported non-invasive Invoke/Toggle pattern",
        "requires_real_input": True,
        "mode": "uia",
        **info,
    }


def ui_set_text(name: str, text: str, window: Optional[str] = None) -> dict[str, Any]:
    """Set editable text without global keyboard input when the control permits it.

    A real HWND gets ``WM_SETTEXT`` first because that does not steal foreground focus.
    Controls without an HWND fall back to UI Automation ValuePattern.
    """
    control = _find_control_by_name(name, window=window)
    if control is None:
        return {"ok": False, "error": "element not found", "mode": "uia"}
    info = _control_dict(control)

    hwnd = int(info.get("handle") or 0)
    if hwnd:
        try:
            buf = ctypes.c_wchar_p(text)
            result = 0
            if _send_message_timeout is not None:
                message_result = ctypes.c_size_t()
                delivered = _send_message_timeout(
                    hwnd, _WM_SETTEXT, 0, ctypes.cast(buf, ctypes.c_void_p).value,
                    0x0002, 2000, ctypes.byref(message_result),  # SMTO_ABORTIFHUNG
                )
                result = int(message_result.value) if delivered else 0
            # Verify through ValuePattern when available. A few controls return 0 even when
            # they accepted WM_SETTEXT, so effect verification is stronger than LRESULT.
            getter = getattr(control, "GetValuePattern", None)
            verified = None
            if getter:
                try:
                    pattern = getter()
                    verified = bool(pattern and pattern.Value == text)
                except Exception:
                    verified = None
            if result or verified:
                return {"ok": True, "mode": "window_message", "effect_verified": verified, **info}
        except Exception:
            pass

    getter = getattr(control, "GetValuePattern", None)
    if getter:
        try:
            pattern = getter()
            if pattern:
                pattern.SetValue(text)
                verified = None
                try:
                    verified = pattern.Value == text
                except Exception:
                    pass
                return {"ok": True, "mode": "uia", "effect_verified": verified, **info}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "mode": "uia", **info}
    return {
        "ok": False,
        "error": "control has no non-invasive text-setting path",
        "requires_real_input": True,
        "mode": "uia",
        **info,
    }


def _point_target(x: int, y: int) -> tuple[int, wintypes.POINT]:
    point = wintypes.POINT(int(x), int(y))
    hwnd = int(_user32.WindowFromPoint(point) or 0)
    if not hwnd:
        raise NativeGuiUnavailable(f"no window at screen coordinate ({x}, {y})")
    client = wintypes.POINT(int(x), int(y))
    if not _user32.ScreenToClient(hwnd, ctypes.byref(client)):
        raise OSError(ctypes.get_last_error(), "ScreenToClient failed")
    return hwnd, client


def _lparam(x: int, y: int) -> int:
    return ((int(y) & 0xFFFF) << 16) | (int(x) & 0xFFFF)


def virtual_move(x: int, y: int) -> dict[str, Any]:
    """Move only the assistant's virtual pointer; the system cursor is untouched."""
    _require_windows()
    hwnd = 0
    title = ""
    class_name = ""
    try:
        hwnd, _client = _point_target(x, y)
        title = _window_text(hwnd)
        class_name = _class_name(hwnd)
    except Exception:
        pass
    with _virtual_lock:
        _virtual_pointer.update({"x": int(x), "y": int(y), "hwnd": int(hwnd)})
    return {
        "x": int(x), "y": int(y), "handle": int(hwnd), "title": title,
        "class_name": class_name, "mode": "virtual", "cursor_moved": False,
    }


def virtual_pointer() -> dict[str, Any]:
    with _virtual_lock:
        return dict(_virtual_pointer)


def background_click(x: Optional[int] = None, y: Optional[int] = None, *,
                     button: str = "left", double: bool = False) -> dict[str, Any]:
    """Send mouse messages directly to the window under a virtual screen point.

    The real Windows cursor is never moved.  Some applications (games, protected or custom
    render surfaces) intentionally ignore posted mouse messages; callers can then choose an
    explicit real-input fallback instead of us stealing the cursor automatically.
    """
    _require_windows()
    if button not in _BUTTON_MESSAGES:
        return {"ok": False, "error": "button must be left|right|middle", "mode": "background"}
    if x is None or y is None:
        state = virtual_pointer()
        x = state["x"] if x is None else x
        y = state["y"] if y is None else y
    try:
        hwnd, client = _point_target(int(x), int(y))
    except Exception as exc:
        return {"ok": False, "error": str(exc), "mode": "background"}

    # For a simple left click, prefer a UI Automation action on the control at the
    # requested point. This is still non-invasive and is more reliable for ordinary
    # Windows buttons than raw mouse messages. Not every provider actually performs the
    # action even when it exposes a pattern, so the effect remains explicitly unverified.
    if button == "left" and not double:
        try:
            auto = _uia_module()
            control = auto.ControlFromPoint(int(x), int(y))
            if control is not None:
                for getter_name, action_name in (("GetInvokePattern", "invoke"), ("GetTogglePattern", "toggle")):
                    getter = getattr(control, getter_name, None)
                    if not getter:
                        continue
                    try:
                        pattern = getter()
                        if pattern:
                            getattr(pattern, "Invoke" if action_name == "invoke" else "Toggle")()
                            info = _control_dict(control)
                            with _virtual_lock:
                                _virtual_pointer.update({"x": int(x), "y": int(y), "hwnd": int(hwnd)})
                            return {
                                "ok": True, "delivered": True, "effect_verified": None,
                                "strategy": f"uia_{action_name}",
                                "x": int(x), "y": int(y), "button": button, "double": False,
                                "mode": "background", "cursor_moved": False, **info,
                            }
                    except Exception:
                        continue
        except Exception:
            pass

    down, up, down_flag = _BUTTON_MESSAGES[button]
    lp = _lparam(client.x, client.y)

    def one_click() -> bool:
        moved = bool(_user32.PostMessageW(hwnd, _WM_MOUSEMOVE, 0, lp))
        pressed = bool(_user32.PostMessageW(hwnd, down, down_flag, lp))
        released = bool(_user32.PostMessageW(hwnd, up, 0, lp))
        return moved and pressed and released

    delivered = one_click()
    if double:
        time.sleep(0.06)
        delivered = one_click() and delivered

    with _virtual_lock:
        _virtual_pointer.update({"x": int(x), "y": int(y), "hwnd": int(hwnd)})
    return {
        "ok": bool(delivered), "delivered": bool(delivered), "effect_verified": None,
        "strategy": "window_message",
        "x": int(x), "y": int(y),
        "client_x": int(client.x), "client_y": int(client.y),
        "button": button, "double": bool(double),
        "handle": int(hwnd), "title": _window_text(hwnd), "class_name": _class_name(hwnd),
        "mode": "background", "cursor_moved": False,
        "note": "delivery succeeded only; the target application may ignore synthetic background input",
    }
