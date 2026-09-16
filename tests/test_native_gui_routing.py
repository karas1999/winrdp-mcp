from __future__ import annotations

from types import SimpleNamespace

from winrdp_mcp.tools import gui, system


class _FakeMCP:
    def __init__(self):
        self.fns = {}

    def tool(self, fn):
        self.fns[fn.__name__] = fn
        return fn


class _LocalCtx:
    def __init__(self):
        self.transport = SimpleNamespace(name="local")

    def transport_for(self, host=None):
        return self.transport

    def exec_ps(self, *args, **kwargs):
        raise AssertionError("local native route must not call PowerShell")


def test_local_gui_tools_prefer_native_without_powershell(monkeypatch):
    ctx = _LocalCtx()
    m = _FakeMCP()
    gui.register(m, ctx)

    monkeypatch.setattr(gui.native_gui, "direct_available", lambda: True)
    monkeypatch.setattr(gui.native_gui, "list_windows", lambda: [{"title": "Test"}])
    monkeypatch.setattr(gui.native_gui, "virtual_move", lambda x, y: {"mode": "virtual", "x": x, "y": y})
    monkeypatch.setattr(
        gui.native_gui,
        "background_click",
        lambda x, y, *, button, double: {
            "mode": "background", "x": x, "y": y, "button": button, "double": double,
        },
    )
    monkeypatch.setattr(
        gui.native_gui,
        "ui_find",
        lambda *, name, control_type, top, window: [
            {"name": name, "type": control_type, "top_n": top, "window": window}
        ],
    )
    monkeypatch.setattr(
        gui.native_gui,
        "ui_invoke",
        lambda name, *, window=None: {"ok": True, "name": name, "window": window},
    )
    monkeypatch.setattr(
        gui.native_gui,
        "ui_set_text",
        lambda name, text, *, window=None: {"ok": True, "name": name, "text": text, "window": window},
    )
    monkeypatch.setattr(
        gui.native_gui,
        "wait_for_window",
        lambda title, *, timeout, interval: {"reached": True, "title": title, "timeout": timeout, "interval": interval},
    )

    assert m.fns["list_windows"]() == [{"title": "Test"}]
    assert m.fns["mouse_move"](12, 34) == {"mode": "virtual", "x": 12, "y": 34}
    assert m.fns["mouse_click"](12, 34, button="right", double=True)["mode"] == "background"
    found = m.fns["ui_find"](name="Save", control_type="Button", top=5, window="Editor")
    assert found == [{"name": "Save", "type": "Button", "top_n": 5, "window": "Editor"}]
    assert m.fns["ui_invoke"]("Save", window="Editor")["window"] == "Editor"
    assert m.fns["ui_set_text"]("Text editor", "hello", window="Notepad")["text"] == "hello"
    assert m.fns["wait_for_window"]("Notepad", timeout=3, interval=1)["reached"] is True

    drag = m.fns["mouse_drag"](0, 0, 10, 10)
    assert drag["requires_real_input"] is True
    assert m.fns["ocr_screen"]()["mode"] == "native"
    assert m.fns["find_and_click"]("anything")["cursor_moved"] is False


def test_local_screenshot_uses_native_capture(monkeypatch):
    ctx = _LocalCtx()
    m = _FakeMCP()
    system.register(m, ctx)

    monkeypatch.setattr(system.native_gui, "direct_available", lambda: True)
    monkeypatch.setattr(system.native_gui, "screenshot_png", lambda: b"PNGDATA")
    monkeypatch.setattr(system, "Image", None)

    result = m.fns["screenshot"]()
    assert result["mode"] == "native"
    assert result["bytes"] == 7
    assert result["format"] == "png"
