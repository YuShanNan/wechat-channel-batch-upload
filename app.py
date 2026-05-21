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
    webview.create_window("视频号上传", URL, width=1100, height=850, resizable=True, text_select=True)
    webview.start()


if __name__ == "__main__":
    main()
