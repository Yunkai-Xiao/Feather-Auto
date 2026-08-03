from __future__ import annotations

import ctypes
import os
import sqlite3
import subprocess
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any


DEFAULT_TASK_PREFIX = "HL Grading: Writing"
DEFAULT_WINDOW_TITLE = "Insightful"
DEFAULT_APP_RELATIVE_PATH = Path("Programs") / "workpuls-agent" / "Workpuls.exe"
DEFAULT_STORAGE_RELATIVE_PATH = Path("workpuls-agent") / "storage"


class InsightfulAutomationError(RuntimeError):
    """Raised when Insightful cannot be started or safely automated."""


def _default_app_path() -> Path:
    override = os.environ.get("INSIGHTFUL_APP_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if not local_app_data:
        raise InsightfulAutomationError("LOCALAPPDATA is unavailable; Insightful could not be located.")
    return Path(local_app_data) / DEFAULT_APP_RELATIVE_PATH


def _default_storage_path() -> Path:
    override = os.environ.get("INSIGHTFUL_STORAGE_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    app_data = os.environ.get("APPDATA", "").strip()
    if not app_data:
        raise InsightfulAutomationError("APPDATA is unavailable; Insightful storage could not be located.")
    return Path(app_data) / DEFAULT_STORAGE_RELATIVE_PATH


def _open_storage(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise InsightfulAutomationError(f"Insightful storage was not found at {path}.")
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
        connection.row_factory = sqlite3.Row
        return connection
    except sqlite3.Error as exc:
        raise InsightfulAutomationError(f"Insightful storage could not be read: {exc}") from exc


def _resolve_target(storage_path: Path, task_prefix: str) -> dict[str, str]:
    prefix = task_prefix.strip()
    if not prefix:
        raise InsightfulAutomationError("The Insightful task prefix is blank.")
    connection: sqlite3.Connection | None = None
    try:
        connection = _open_storage(storage_path)
        rows = connection.execute(
            "SELECT id, name, projectId FROM tasks WHERE lower(name) LIKE lower(?)",
            (prefix + "%",),
        ).fetchall()
        unique_tasks = {
            (str(row["id"]), str(row["projectId"])): {
                "task_id": str(row["id"]),
                "task_name": str(row["name"]),
                "project_id": str(row["projectId"]),
            }
            for row in rows
            if row["id"] and row["projectId"]
        }
        if not unique_tasks:
            raise InsightfulAutomationError(
                f'No Insightful task starts with "{prefix}". Sync Insightful and try again.'
            )
        if len(unique_tasks) != 1:
            names = sorted({task["task_name"] for task in unique_tasks.values()})
            raise InsightfulAutomationError(
                f'The Insightful task prefix "{prefix}" matched multiple tasks: {", ".join(names[:3])}'
            )
        target = next(iter(unique_tasks.values()))
        project = connection.execute(
            "SELECT name FROM projects WHERE id = ? LIMIT 1",
            (target["project_id"],),
        ).fetchone()
    except sqlite3.Error as exc:
        raise InsightfulAutomationError(f"Insightful task data could not be read: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()
    if not project or not project["name"]:
        raise InsightfulAutomationError(
            f'Insightful project {target["project_id"]} was not found for the target task.'
        )
    return {**target, "project_name": str(project["name"])}


def _active_windows(storage_path: Path) -> list[dict[str, str]]:
    connection: sqlite3.Connection | None = None
    try:
        connection = _open_storage(storage_path)
        rows = connection.execute(
            "SELECT id, projectId, taskId FROM windows WHERE end = 0 ORDER BY start DESC"
        ).fetchall()
    except sqlite3.Error as exc:
        raise InsightfulAutomationError(f"Insightful active timer state could not be read: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()
    return [
        {
            "window_id": str(row["id"] or ""),
            "project_id": str(row["projectId"] or ""),
            "task_id": str(row["taskId"] or ""),
        }
        for row in rows
    ]


def _matching_active_window(storage_path: Path, target: dict[str, str]) -> dict[str, str] | None:
    active = _active_windows(storage_path)
    if not active:
        return None
    matching = [
        window
        for window in active
        if window["project_id"] == target["project_id"] and window["task_id"] == target["task_id"]
    ]
    if matching and len(matching) == len(active):
        return matching[0]
    current = active[0]
    raise InsightfulAutomationError(
        "Insightful is already timing another project/task "
        f'(project={current["project_id"] or "unknown"}, task={current["task_id"] or "unknown"}).'
    )


if os.name == "nt":
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", wintypes.LONG),
            ("top", wintypes.LONG),
            ("right", wintypes.LONG),
            ("bottom", wintypes.LONG),
        ]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD),
            ("biWidth", wintypes.LONG),
            ("biHeight", wintypes.LONG),
            ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", wintypes.LONG),
            ("biYPelsPerMeter", wintypes.LONG),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_size_t),
        ]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_size_t),
        ]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [
            ("uMsg", wintypes.DWORD),
            ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        ]

    class INPUT_VALUE(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("value",)
        _fields_ = [("type", wintypes.DWORD), ("value", INPUT_VALUE)]

    user32.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM), wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    user32.SetCursorPos.restype = wintypes.BOOL
    user32.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_void_p]
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.ReleaseDC.restype = ctypes.c_int
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.BitBlt.argtypes = [
        wintypes.HDC,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HDC,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.DWORD,
    ]
    gdi32.BitBlt.restype = wintypes.BOOL
    gdi32.GetDIBits.argtypes = [
        wintypes.HDC,
        wintypes.HBITMAP,
        wintypes.UINT,
        wintypes.UINT,
        ctypes.c_void_p,
        ctypes.POINTER(BITMAPINFO),
        wintypes.UINT,
    ]
    gdi32.GetDIBits.restype = ctypes.c_int
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.DeleteDC.restype = wintypes.BOOL


def _require_windows() -> None:
    if os.name != "nt":
        raise InsightfulAutomationError("Insightful desktop automation is only supported on Windows.")


def _find_window(title: str = DEFAULT_WINDOW_TITLE) -> int | None:
    _require_windows()
    matches: list[int] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, len(buffer))
        if buffer.value.strip().casefold() == title.casefold():
            matches.append(int(hwnd))
            return False
        return True

    user32.EnumWindows(callback, 0)
    return matches[0] if matches else None


def _ensure_window(app_path: Path, timeout_seconds: float = 15.0) -> int:
    hwnd = _find_window()
    if hwnd:
        return hwnd
    if not app_path.is_file():
        raise InsightfulAutomationError(f"Insightful was not found at {app_path}.")
    try:
        subprocess.Popen([str(app_path)], close_fds=True)
    except OSError as exc:
        raise InsightfulAutomationError(f"Insightful could not be opened: {exc}") from exc
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        time.sleep(0.25)
        hwnd = _find_window()
        if hwnd:
            return hwnd
    raise InsightfulAutomationError("Insightful opened, but its window did not become ready within 15 seconds.")


def _window_rect(hwnd: int) -> RECT:
    rect = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise InsightfulAutomationError("Insightful window bounds could not be read.")
    if rect.right <= rect.left or rect.bottom <= rect.top:
        raise InsightfulAutomationError("Insightful returned invalid window bounds.")
    return rect


def _focus_window(hwnd: int) -> None:
    # A brief Alt key event lets a foreground desktop process legally transfer focus on Windows.
    user32.keybd_event(0x12, 0, 0, None)
    user32.keybd_event(0x12, 0, 0x0002, None)
    user32.ShowWindow(hwnd, 9)
    user32.BringWindowToTop(hwnd)
    if not user32.SetForegroundWindow(hwnd):
        raise InsightfulAutomationError("Insightful could not be brought to the foreground.")


def _scaled_point(rect: RECT, x: int, y: int) -> tuple[int, int]:
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    return rect.left + round(x * width / 384), rect.top + round(y * height / 720)


def _click_relative(hwnd: int, x: int, y: int) -> None:
    rect = _window_rect(hwnd)
    screen_x, screen_y = _scaled_point(rect, x, y)
    if not user32.SetCursorPos(screen_x, screen_y):
        raise InsightfulAutomationError("The mouse pointer could not be positioned over Insightful.")
    user32.mouse_event(0x0002, 0, 0, 0, None)
    user32.mouse_event(0x0004, 0, 0, 0, None)


def _send_unicode(text: str) -> None:
    for character in text:
        code = ord(character)
        if code > 0xFFFF:
            raise InsightfulAutomationError("The Insightful search text contains an unsupported character.")
        events = (INPUT * 2)(
            INPUT(type=1, ki=KEYBDINPUT(0, code, 0x0004, 0, 0)),
            INPUT(type=1, ki=KEYBDINPUT(0, code, 0x0004 | 0x0002, 0, 0)),
        )
        if user32.SendInput(2, events, ctypes.sizeof(INPUT)) != 2:
            raise InsightfulAutomationError("Text could not be entered into the Insightful search box.")


def _replace_text(text: str) -> None:
    user32.keybd_event(0x11, 0, 0, None)
    user32.keybd_event(0x41, 0, 0, None)
    user32.keybd_event(0x41, 0, 0x0002, None)
    user32.keybd_event(0x11, 0, 0x0002, None)
    _send_unicode(text)


def _capture_window(hwnd: int) -> tuple[int, int, bytes]:
    rect = _window_rect(hwnd)
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    screen_dc = user32.GetDC(0)
    memory_dc = gdi32.CreateCompatibleDC(screen_dc)
    bitmap = gdi32.CreateCompatibleBitmap(screen_dc, width, height)
    previous = gdi32.SelectObject(memory_dc, bitmap)
    try:
        if not gdi32.BitBlt(memory_dc, 0, 0, width, height, screen_dc, rect.left, rect.top, 0x00CC0020):
            raise InsightfulAutomationError("Insightful window pixels could not be captured.")
        info = BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = 0
        buffer = (ctypes.c_ubyte * (width * height * 4))()
        if not gdi32.GetDIBits(memory_dc, bitmap, 0, height, buffer, ctypes.byref(info), 0):
            raise InsightfulAutomationError("Insightful window pixels could not be decoded.")
        return width, height, bytes(buffer)
    finally:
        gdi32.SelectObject(memory_dc, previous)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(0, screen_dc)


def _purple_components(width: int, height: int, pixels: bytes) -> list[dict[str, int]]:
    x_min = int(width * 0.72)
    x_max = width - 8
    y_min = int(height * 0.18)
    y_max = int(height * 0.86)
    purple: set[tuple[int, int]] = set()
    for y in range(y_min, y_max):
        row_offset = y * width * 4
        for x in range(x_min, x_max):
            offset = row_offset + x * 4
            blue, green, red = pixels[offset], pixels[offset + 1], pixels[offset + 2]
            if (
                blue >= 180
                and 65 <= red <= 190
                and 35 <= green <= 180
                and blue - red >= 30
                and blue - green >= 25
            ):
                purple.add((x, y))

    components: list[dict[str, int]] = []
    while purple:
        seed = purple.pop()
        stack = [seed]
        points = [seed]
        while stack:
            x, y = stack.pop()
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    if not dx and not dy:
                        continue
                    neighbor = (x + dx, y + dy)
                    if neighbor in purple:
                        purple.remove(neighbor)
                        stack.append(neighbor)
                        points.append(neighbor)
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        components.append(
            {
                "count": len(points),
                "left": min(xs),
                "right": max(xs),
                "top": min(ys),
                "bottom": max(ys),
            }
        )
    return components


def _find_purple_play_button(width: int, height: int, pixels: bytes) -> tuple[int, int]:
    candidates = []
    for component in _purple_components(width, height, pixels):
        component_width = component["right"] - component["left"] + 1
        component_height = component["bottom"] - component["top"] + 1
        if component["count"] >= 40 and 25 <= component_width <= 65 and 25 <= component_height <= 65:
            candidates.append(component)
    if not candidates:
        raise InsightfulAutomationError(
            "The purple Insightful play button was not found after filtering the target task."
        )
    candidate = min(candidates, key=lambda item: (item["top"], -item["count"]))
    return (
        round((candidate["left"] + candidate["right"]) / 2),
        round((candidate["top"] + candidate["bottom"]) / 2),
    )


def start_insightful_timer(
    task_prefix: str = DEFAULT_TASK_PREFIX,
    *,
    app_path: Path | None = None,
    storage_path: Path | None = None,
    verify_timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    """Open Insightful, start the matching task, and verify its active timer row."""

    _require_windows()
    resolved_storage = storage_path or _default_storage_path()
    target = _resolve_target(resolved_storage, task_prefix)
    if _matching_active_window(resolved_storage, target):
        return {"state": "already_running", **target}

    hwnd = _ensure_window(app_path or _default_app_path())
    _focus_window(hwnd)
    time.sleep(0.35)

    # On a task page this first click is the Projects back link. On the projects page
    # the same position merely focuses the project search box, so the sequence is safe
    # from either normal timer route.
    _click_relative(hwnd, 52, 66)
    time.sleep(0.55)
    _click_relative(hwnd, 150, 76)
    _replace_text(target["project_name"])
    time.sleep(0.75)
    _click_relative(hwnd, 180, 220)
    time.sleep(0.75)
    _click_relative(hwnd, 160, 169)
    _replace_text(task_prefix.strip())
    time.sleep(0.75)

    width, height, pixels = _capture_window(hwnd)
    play_x, play_y = _find_purple_play_button(width, height, pixels)
    rect = _window_rect(hwnd)
    baseline_x = round(play_x * 384 / (rect.right - rect.left))
    baseline_y = round(play_y * 720 / (rect.bottom - rect.top))
    _click_relative(hwnd, baseline_x, baseline_y)

    deadline = time.monotonic() + verify_timeout_seconds
    while time.monotonic() < deadline:
        time.sleep(0.25)
        active = _matching_active_window(resolved_storage, target)
        if active:
            return {"state": "started", **target}
    raise InsightfulAutomationError(
        "The Insightful play button was clicked, but the target timer did not become active."
    )
