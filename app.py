"""
视频号上传 — 独立桌面窗口启动器
使用 pywebview (Edge WebView2)，需 Python 3.11+
启动: python app.py
"""
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

PORT = 5050
URL = f"http://127.0.0.1:{PORT}"

# 分发兼容：EXE 自带 Chromium 浏览器，运行时指定路径
if getattr(sys, 'frozen', False):
    _bundled = Path(sys._MEIPASS) / "ms-playwright"
    if _bundled.exists():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(_bundled)


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

            icon.stop()
            window.destroy()
            sys.exit(0)

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
        import web_server
        web_server._main_window = window
        web_server._tray_exit = lambda: on_tray_exit(None, None)

        # Run tray in daemon thread so it doesn't block webview.start()
        threading.Thread(target=tray_icon.run, daemon=True).start()

    except ImportError:
        pass  # tray is optional, skip if pystray/Pillow not installed
    # ---- End Tray ----

    webview.start()


if __name__ == "__main__":
    main()
