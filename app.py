"""
视频号上传 — 独立桌面窗口启动器
使用 pywebview (Edge WebView2)，需 Python 3.11+
启动: python app.py
"""
import threading
import time

from server import app

PORT = 5050
URL = f"http://127.0.0.1:{PORT}"


def main():
    import webview

    def run_flask():
        app.run(host="127.0.0.1", port=PORT, debug=False)

    threading.Thread(target=run_flask, daemon=True).start()
    time.sleep(1)

    webview.create_window("视频号上传", URL, width=1100, height=850, resizable=True)
    webview.start()


if __name__ == "__main__":
    main()
