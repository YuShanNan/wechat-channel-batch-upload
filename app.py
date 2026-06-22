"""
视频号上传 — 独立桌面窗口启动器
使用 pywebview (Edge WebView2)，需 Python 3.11+
启动: python app.py
"""
import ctypes
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

PORT = 5050
URL = f"http://127.0.0.1:{PORT}"

# ===== 单实例：已运行时提到最前，不打开新窗口 =====
def _bring_existing_to_front():
    """通过 Flask API 让已有进程自己显示窗口（最可靠）"""
    try:
        import json
        resp = urllib.request.urlopen(f"{URL}/api/window/show", timeout=2)
        data = json.loads(resp.read())
        return data.get("ok", False)
    except Exception:
        pass

    # 兜底：Win32 方式
    user32 = ctypes.windll.user32
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_ulong, ctypes.c_ulong)
    found = []

    def _enum_cb(hwnd, _lparam):
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if "视频号上传" in buf.value:
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(WNDENUMPROC(_enum_cb), 0)
    if not found:
        return False
    hwnd = found[0]
    user32.ShowWindow(hwnd, 5)  # SW_SHOW
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    kernel32 = ctypes.windll.kernel32
    fg_thread = user32.GetWindowThreadProcessId(hwnd, 0)
    cur_thread = kernel32.GetCurrentThreadId()
    user32.AttachThreadInput(cur_thread, fg_thread, True)
    user32.SetForegroundWindow(hwnd)
    user32.BringWindowToTop(hwnd)
    user32.AttachThreadInput(cur_thread, fg_thread, False)
    return True

_MUTEX_NAME = "Global\\WeChatVideoUploader_SingleInstance_9a3f2"
_kernel32 = ctypes.windll.kernel32
_kernel32.CreateMutexW(None, False, _MUTEX_NAME)
if _kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
    if _bring_existing_to_front():
        sys.exit(0)
    # 如果 API 和 Win32 都失败，尝试等待一下再退出
    time.sleep(0.5)
    sys.exit(1)
# ===== 单实例结束 =====

# 分发兼容：EXE 自带 CloakBrowser + Playwright driver
if getattr(sys, 'frozen', False):
    _cb = Path(sys._MEIPASS) / "cloakbrowser" / "chrome.exe"
    if _cb.exists():
        os.environ["CLOAKBROWSER_BINARY_PATH"] = str(_cb)

    # 修正 playwright driver 路径（PYZ 归档中路径解析会失败）
    try:
        import playwright._impl._driver as _pw_driver
        def _patched_compute_driver():
            driver_path = Path(sys._MEIPASS) / "playwright" / "driver"
            cli_path = str(driver_path / "package" / "cli.js")
            if sys.platform == "win32":
                return (
                    os.environ.get("PLAYWRIGHT_NODEJS_PATH", str(driver_path / "node.exe")),
                    cli_path,
                )
            return (os.environ.get("PLAYWRIGHT_NODEJS_PATH", str(driver_path / "node")), cli_path)
        _pw_driver.compute_driver_executable = _patched_compute_driver
    except Exception:
        pass

    # 诊断日志
    _diag_msgs = [
        f"[DIAG] MEIPASS={sys._MEIPASS}",
        f"[DIAG] CLOAKBROWSER_BINARY_PATH={os.environ.get('CLOAKBROWSER_BINARY_PATH', 'UNSET')}",
        f"[DIAG] chrome.exe exists={_cb.exists()}",
    ]
    _log_dir = Path(sys.executable).parent / "data" / "logs"
    _log_dir.mkdir(parents=True, exist_ok=True)
    _diag_log = _log_dir / "diag.log"
    with open(_diag_log, "w", encoding="utf-8") as _f:
        _f.write("\n".join(_diag_msgs))


def wait_for_flask():
    for _ in range(60):
        try:
            urllib.request.urlopen(URL, timeout=0.5)
            return
        except Exception:
            time.sleep(0.05)
    raise RuntimeError(f"Flask 服务启动失败，请检查端口 {PORT} 是否被占用")


def main():
    from web_server import app  # 延迟导入，窗口先出现

    if getattr(sys, 'frozen', False):
        from flask import request as flask_request
        _tpl = Path(sys._MEIPASS) / "templates"
        _index_html = (_tpl / "index.html").read_text(encoding="utf-8")

        @app.before_request
        def _serve_index():
            if flask_request.path == "/":
                return _index_html, 200, {"Content-Type": "text/html; charset=utf-8"}
            return None

    def run_flask():
        app.run(host="127.0.0.1", port=PORT, debug=False)

    threading.Thread(target=run_flask, daemon=True).start()
    wait_for_flask()

    import webview
    window = webview.create_window("视频号上传", URL, width=1100, height=850, resizable=True, text_select=True)

    # ---- Tray ----
    try:
        from PIL import Image, ImageDraw
        import pystray

        # Tray icon: load ICO via BytesIO (avoid Permission denied on bundled file)
        icon_path = (Path(sys._MEIPASS) / "icon.ico") if hasattr(sys, '_MEIPASS') else (Path(__file__).parent / "icon.ico")
        icon_img = None
        if icon_path.exists():
            try:
                from io import BytesIO
                raw = Image.open(BytesIO(icon_path.read_bytes()))
                raw.load()
                icon_img = raw.convert("RGBA").resize((64, 64), Image.LANCZOS)
            except Exception:
                pass
        if icon_img is None:
            icon_img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            ImageDraw.Draw(icon_img).ellipse([2, 2, 62, 62], fill=(130, 198, 83))

        def on_tray_show(icon, item):
            window.show()

        def on_tray_exit(icon, item):
            # Check if uploads are running
            try:
                import urllib.request as _req
                import json as _json
                resp = _req.urlopen(f"{URL}/api/upload/status/all", timeout=2)
                data = _json.loads(resp.read())
                running = any(v.get("running") for v in data.values())
            except Exception:
                running = False

            if running:
                import ctypes
                result = ctypes.windll.user32.MessageBoxW(0,
                    "有上传任务正在进行中，确定要退出吗？", "视频号上传", 1)  # MB_OKCANCEL
                if result != 1:  # IDOK
                    return

            # 优雅关闭：取消上传 → 等待完成 → 关闭浏览器 → 清理锁文件
            try:
                web_server._graceful_shutdown(timeout=10)
            except Exception:
                pass

            icon.stop()
            try:
                window.destroy()
            except Exception:
                pass
            os._exit(0)

        tray_icon = pystray.Icon(
            "wechat_uploader",
            icon_img,
            "视频号上传",
            menu=pystray.Menu(
                pystray.MenuItem("显示窗口", on_tray_show, default=True),
                pystray.MenuItem("退出", on_tray_exit),
            ),
        )

        # Override window close: always minimize, frontend shows choice dialog
        def _on_closing():
            window.hide()
            return False

        window.events.closing += _on_closing

        # Expose window + tray exit to web_server
        # 前端已验证过上传状态，后端不再重复 HTTP 检查（避免 Flask 死锁）
        def _force_exit():
            # 优雅关闭：取消上传 → 等待完成 → 关闭浏览器 → 清理锁文件
            try:
                web_server._graceful_shutdown(timeout=10)
            except Exception:
                pass
            icon.stop()
            try:
                window.destroy()
            except Exception:
                pass
            os._exit(0)

        import web_server
        web_server._main_window = window
        web_server._tray_exit = _force_exit

        # Run tray in daemon thread so it doesn't block webview.start()
        threading.Thread(target=tray_icon.run, daemon=True).start()

    except ImportError:
        pass  # tray is optional, skip if pystray/Pillow not installed
    # ---- End Tray ----

    webview.start()


if __name__ == "__main__":
    main()
