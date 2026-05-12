# 视频号上传 — 独立桌面窗口

## Context
当前工具通过 Flask + 浏览器访问。用户希望将其包装为独立桌面窗口应用（pywebview），无需打开外部浏览器。

## 方案
- **pywebview**: Python 库，基于 Windows 自带的 Edge WebView2
- Flask 在后台线程运行，pywebview 加载 `http://127.0.0.1:5050/`
- 命令行模式 `python server.py` 保持不变

## 文件变更

### 1. `server.py` — 抽离 `create_app()`
```python
def create_app():
    return app  # Flask app 实例
```
`main()` 调用 `create_app()` 保持不变。

### 2. `app.py` — **新文件**，桌面入口
```python
import threading
import webview
from server import create_app, main as _parse_args

def start_server():
    app = create_app()
    app.run(host="127.0.0.1", port=5050, debug=False)

if __name__ == "__main__":
    threading.Thread(target=start_server, daemon=True).start()
    webview.create_window("视频号上传", "http://127.0.0.1:5050/", width=1100, height=750)
    webview.start()
```

### 3. `requirements.txt` — 加 `pywebview`
```
pywebview
```

## 不变
- 所有 API 端点、前端 HTML/CSS/JS、uploader.py、accounts.py 零改动
- `python server.py --port 5050` 命令行模式照常使用

## 验证
1. `python app.py` → 弹出独立窗口，显示上传界面
2. 窗口关闭后 Flask 线程自动退出（daemon=True）
3. 账号管理、上传、debug 开关功能正常
