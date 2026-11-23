"""Windows system-tray icon using only ctypes (no pywin32 / pystray needed).

The icon lives on its own thread with a hidden window and a Win32 message
loop. Clicks are reported through `on_action(name)` *from that thread*, so the
callback must be thread-safe (the app just puts them on its queue).

Actions: "toggle" (left click), "show", "pause", "exit".
"""
from __future__ import annotations

import ctypes
import sys
import threading
from ctypes import wintypes as W

AVAILABLE = sys.platform == "win32"

if AVAILABLE:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, W.HWND, W.UINT, W.WPARAM, W.LPARAM)

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [("style", W.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int), ("hInstance", W.HINSTANCE), ("hIcon", W.HICON),
                    ("hCursor", W.HANDLE), ("hbrBackground", W.HBRUSH),
                    ("lpszMenuName", W.LPCWSTR), ("lpszClassName", W.LPCWSTR)]

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", W.DWORD), ("Data2", W.WORD), ("Data3", W.WORD),
                    ("Data4", ctypes.c_ubyte * 8)]

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [("cbSize", W.DWORD), ("hWnd", W.HWND), ("uID", W.UINT), ("uFlags", W.UINT),
                    ("uCallbackMessage", W.UINT), ("hIcon", W.HICON), ("szTip", W.WCHAR * 128),
                    ("dwState", W.DWORD), ("dwStateMask", W.DWORD), ("szInfo", W.WCHAR * 256),
                    ("uVersion", W.UINT), ("szInfoTitle", W.WCHAR * 64), ("dwInfoFlags", W.DWORD),
                    ("guidItem", GUID), ("hBalloonIcon", W.HICON)]

    def _sig(fn, res, *args):
        fn.restype, fn.argtypes = res, list(args)

    _sig(user32.DefWindowProcW, LRESULT, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
    _sig(user32.RegisterClassW, W.ATOM, ctypes.POINTER(WNDCLASSW))
    _sig(user32.CreateWindowExW, W.HWND, W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD, ctypes.c_int,
         ctypes.c_int, ctypes.c_int, ctypes.c_int, W.HWND, W.HMENU, W.HINSTANCE, W.LPVOID)
    _sig(user32.DestroyWindow, W.BOOL, W.HWND)
    _sig(user32.GetMessageW, W.BOOL, ctypes.POINTER(W.MSG), W.HWND, W.UINT, W.UINT)
    _sig(user32.TranslateMessage, W.BOOL, ctypes.POINTER(W.MSG))
    _sig(user32.DispatchMessageW, LRESULT, ctypes.POINTER(W.MSG))
    _sig(user32.PostMessageW, W.BOOL, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
    _sig(user32.PostQuitMessage, None, ctypes.c_int)
    _sig(user32.RegisterWindowMessageW, W.UINT, W.LPCWSTR)
    _sig(user32.LoadImageW, W.HANDLE, W.HINSTANCE, W.LPCWSTR, W.UINT, ctypes.c_int, ctypes.c_int, W.UINT)
    _sig(user32.LoadIconW, W.HICON, W.HINSTANCE, W.LPVOID)
    _sig(user32.DestroyIcon, W.BOOL, W.HICON)
    _sig(user32.GetSystemMetrics, ctypes.c_int, ctypes.c_int)
    _sig(user32.CreatePopupMenu, W.HMENU)
    _sig(user32.AppendMenuW, W.BOOL, W.HMENU, W.UINT, ctypes.c_size_t, W.LPCWSTR)
    _sig(user32.SetMenuDefaultItem, W.BOOL, W.HMENU, W.UINT, W.UINT)
    _sig(user32.TrackPopupMenu, ctypes.c_int, W.HMENU, W.UINT, ctypes.c_int, ctypes.c_int,
         ctypes.c_int, W.HWND, W.LPVOID)
    _sig(user32.DestroyMenu, W.BOOL, W.HMENU)
    _sig(user32.GetCursorPos, W.BOOL, ctypes.POINTER(W.POINT))
    _sig(user32.SetForegroundWindow, W.BOOL, W.HWND)
    _sig(shell32.Shell_NotifyIconW, W.BOOL, W.DWORD, ctypes.POINTER(NOTIFYICONDATAW))
    _sig(kernel32.GetModuleHandleW, W.HMODULE, W.LPCWSTR)

WM_DESTROY, WM_CLOSE, WM_NULL = 0x0002, 0x0010, 0x0000
WM_LBUTTONUP, WM_LBUTTONDBLCLK, WM_RBUTTONUP, WM_CONTEXTMENU = 0x0202, 0x0203, 0x0205, 0x007B
WM_APP = 0x8000
WM_TRAY, WM_APP_UPDATE, WM_APP_QUIT, WM_APP_NOTIFY = WM_APP + 1, WM_APP + 2, WM_APP + 3, WM_APP + 4
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x1, 0x2, 0x4, 0x10
NIIF_INFO, NIIF_NOSOUND = 0x1, 0x10
MF_STRING, MF_SEPARATOR, MF_CHECKED, MF_GRAYED = 0x0, 0x800, 0x8, 0x1
TPM_RIGHTBUTTON, TPM_RETURNCMD, TPM_NONOTIFY = 0x2, 0x100, 0x80
IMAGE_ICON, LR_LOADFROMFILE = 1, 0x10
SM_CXSMICON, SM_CYSMICON = 49, 50
IDI_APPLICATION = 32512

_CLASS_NAME = "BinancePriceViewerTray"
_MENU = {1: "show", 2: "pause", 3: "exit"}


class TrayIcon:
    def __init__(self, icon_path: str, tooltip: str, on_action):
        self.icon_path = icon_path
        self.on_action = on_action
        self._tip = tooltip
        self._balloon = ("", "")
        self.window_visible = True   # menu text: "Hide" vs "Show"
        self.paused = False          # menu check mark
        self._hwnd = None
        self._hicon = None
        self._ready = threading.Event()
        self._ok = False
        self._thread: threading.Thread | None = None
        self._wndproc = None  # keep the callback alive

    # ---- public API (any thread) ----
    def start(self) -> bool:
        if not AVAILABLE:
            return False
        self._thread = threading.Thread(target=self._run, name="tray", daemon=True)
        self._thread.start()
        self._ready.wait(5)
        return self._ok

    def set_tooltip(self, text: str):
        text = text[:127]
        if text != self._tip:
            self._tip = text
            self._post(WM_APP_UPDATE)

    def notify(self, title: str, text: str):
        self._balloon = (title[:63], text[:255])
        self._post(WM_APP_NOTIFY)

    def stop(self):
        if self._hwnd:
            self._post(WM_APP_QUIT)
            if self._thread and self._thread is not threading.current_thread():
                self._thread.join(2)

    # ---- internals (tray thread) ----
    def _post(self, msg):
        if self._hwnd:
            user32.PostMessageW(self._hwnd, msg, 0, 0)

    def _nid(self, flags: int) -> "NOTIFYICONDATAW":
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = 1
        nid.uFlags = flags
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self._hicon
        nid.szTip = self._tip
        return nid

    def _add(self):
        return shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self._nid(NIF_MESSAGE | NIF_ICON | NIF_TIP)))

    def _run(self):
        try:
            hinst = kernel32.GetModuleHandleW(None)
            self._wndproc = WNDPROC(self._proc)
            wc = WNDCLASSW()
            wc.lpfnWndProc = self._wndproc
            wc.hInstance = hinst
            wc.lpszClassName = _CLASS_NAME
            user32.RegisterClassW(ctypes.byref(wc))  # fails harmlessly if already registered
            self._taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")
            self._hwnd = user32.CreateWindowExW(0, _CLASS_NAME, "Binance Price Viewer tray", 0,
                                                0, 0, 0, 0, None, None, hinst, None)
            if not self._hwnd:
                raise OSError(ctypes.get_last_error())
            cx, cy = user32.GetSystemMetrics(SM_CXSMICON), user32.GetSystemMetrics(SM_CYSMICON)
            self._hicon = user32.LoadImageW(None, self.icon_path, IMAGE_ICON, cx, cy, LR_LOADFROMFILE)
            if not self._hicon:
                self._hicon = user32.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))
            self._ok = bool(self._add())
        except Exception:
            self._ok = False
        finally:
            self._ready.set()
        if not self._ok:
            return
        msg = W.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        if self._hicon:
            user32.DestroyIcon(self._hicon)
        self._hwnd = None

    def _proc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TRAY:
                event = lparam & 0xFFFF
                if event == WM_LBUTTONUP:
                    self.on_action("toggle")
                elif event in (WM_RBUTTONUP, WM_CONTEXTMENU):
                    self._show_menu()
                return 0
            if msg == WM_APP_UPDATE:
                shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self._nid(NIF_TIP)))
                return 0
            if msg == WM_APP_NOTIFY:
                nid = self._nid(NIF_INFO)
                nid.szInfoTitle, nid.szInfo = self._balloon
                nid.dwInfoFlags = NIIF_INFO | NIIF_NOSOUND
                shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
                return 0
            if msg == WM_APP_QUIT:
                shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid(0)))
                user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
            if msg == getattr(self, "_taskbar_created", -1):
                self._add()  # Explorer restarted: put the icon back
                return 0
        except Exception:
            pass
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _show_menu(self):
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, MF_STRING, 1, "Hide window" if self.window_visible else "Show window")
        user32.AppendMenuW(menu, MF_STRING | (MF_CHECKED if self.paused else 0), 2, "Pause updates")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, 3, "Exit")
        user32.SetMenuDefaultItem(menu, 1, 0)
        pt = W.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(self._hwnd)  # so the menu closes when clicking elsewhere
        cmd = user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY,
                                    pt.x, pt.y, 0, self._hwnd, None)
        user32.PostMessageW(self._hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(menu)
        if cmd in _MENU:
            action = _MENU[cmd]
            self.on_action("toggle" if action == "show" else action)
