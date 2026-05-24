# 上传增强功能实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 上传失败自动重试、完成通知提醒、最小化到托盘

**Architecture:** 三个独立功能——重试逻辑在 `web_server.py` 上传循环内、通知在前端控制+后端触发、托盘在 `app.py` 生命周期管理

**Tech Stack:** winsound, plyer, pystray, Pillow

---

### Task 1: 安装新依赖

**Files:** None (pip install)

- [ ] **Step 1: Install pystray and Pillow**

```bash
pip install pystray Pillow plyer
```

- [ ] **Step 2: Verify imports work**

```bash
python -c "import pystray; from PIL import Image; import plyer; import winsound; print('OK')"
```

---

### Task 2: 失败自动重试

**Files:**
- Modify: `web_server.py` — upload loop (lines 224-246)

- [ ] **Step 1: 修改 upload_single 调用段，包裹重试逻辑**

Replace lines 222-246:

```python
                state["_current_uploader"] = uploader
                uploader._upload_progress = 0

                max_retries = 3
                result = None
                for retry in range(max_retries + 1):
                    result = await uploader.upload_single(
                        video_path=video["video_path"],
                        title=title,
                        description=video.get("description", ""),
                        cover_path=video.get("cover_path", ""),
                        short_drama_name=video.get("short_drama_name", ""),
                        publish_time=video.get("publish_time", ""),
                        location=video.get("location", "none"),
                    )
                    if result.get("status") == WeChatUploader.STATUS_PUBLISHED:
                        break
                    if cancel_ev.is_set():
                        break
                    if skip_ev.is_set():
                        break
                    if retry < max_retries:
                        state["status"] = f"重试中 ({retry + 1}/{max_retries})…"
                        _add_log(account_name, f"[重试] {title} ({retry + 1}/{max_retries})")
                        uploader._upload_progress = 0

                if skip_ev.is_set():
                    skip_ev.clear()
                    state["_video_queue"][i]["_status"] = "skipped"
                    _add_log(account_name, f"[跳过] {title}")
                    state["progress"] = int((i + 1) / total * 100)
                    continue

                state["_video_queue"][i]["_status"] = result.get("status", WeChatUploader.STATUS_UNKNOWN)
                if result.get("status") == WeChatUploader.STATUS_PUBLISHED:
                    _add_log(account_name, f"[成功] {title}")
                else:
                    _add_log(account_name, f"[失败] {title}: {result.get('error', '')}")
                state["progress"] = int((i + 1) / total * 100)
```

- [ ] **Step 2: 验证语法**

```bash
python -m py_compile E:/project/wechat_chanel/web_server.py && echo OK
```

---

### Task 3: 声音+通知

**Files:**
- Modify: `web_server.py` — 新增 sound config 端点 + 上传完成通知
- Modify: `templates/index.html` — 提示音开关 UI

- [ ] **Step 1: 在 web_server.py 添加全局变量和 config 端点**

Insert after the debug endpoints (after line 291):

```python
_sound_enabled = True


@app.route("/api/config/sound", methods=["GET"])
def api_sound_get():
    return jsonify({"enabled": _sound_enabled})


@app.route("/api/config/sound", methods=["POST"])
def api_sound_set():
    global _sound_enabled
    data = request.get_json()
    _sound_enabled = bool(data.get("enabled", True))
    return jsonify({"enabled": _sound_enabled})
```

- [ ] **Step 2: 在上传完成后触发通知**

Replace lines 252-254 (the normal completion block):

```python
            if not cancel_ev.is_set():
                state["status"] = "全部完成"
                state["progress"] = 100
                _notify_upload_complete(account_name, total)
```

Add helper function near `_compute_combined_progress`:

```python
def _notify_upload_complete(account_name: str, total: int):
    """Upload complete: system notification + optional sound."""
    state = _get_or_create_account_state(account_name)
    queue = state.get("_video_queue", [])
    failed = sum(1 for v in queue if v.get("_status") != WeChatUploader.STATUS_PUBLISHED and v.get("_status") != "skipped")
    success = total - failed

    msg = f"{success} 个视频上传完成（共 {total} 个）"
    if failed > 0:
        msg += f"，{failed} 个失败"

    # Windows notification
    try:
        from plyer import notification
        notification.notify(
            title="视频号上传",
            message=msg,
            app_name="视频号上传",
            timeout=5,
        )
    except Exception:
        pass

    # System beep
    if _sound_enabled:
        try:
            import winsound
            winsound.MessageBeep(0x00000040)  # MB_ICONASTERISK
        except Exception:
            pass
```

- [ ] **Step 3: 前端添加提示音开关 UI**

In `templates/index.html`, in `renderMainContent()`, add after the debug toggle link (inside the card area, after line 437):

```html
<span class="debug-toggle" id="sound-toggle" onclick="toggleSound()" title="关闭提示音">提示音</span>
```

CSS: reuse `.debug-toggle` class since it has the same style.

Add JS function:

```javascript
let soundEnabled = true;
function toggleSound() {
  soundEnabled = !soundEnabled;
  const el = document.getElementById('sound-toggle');
  if (el) { el.classList.toggle('active', soundEnabled); el.textContent = soundEnabled ? '提示音' : '已静音'; }
  localStorage.setItem('soundEnabled', soundEnabled ? '1' : '0');
  fetch('/api/config/sound', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled: soundEnabled }) });
}
```

Add init in the IIFE at page load (after line ~1094, alongside debug init):

```javascript
  soundEnabled = localStorage.getItem('soundEnabled') !== '0';
  const el = document.getElementById('sound-toggle');
  if (el) { el.classList.toggle('active', soundEnabled); el.textContent = soundEnabled ? '提示音' : '已静音'; }
  fetch('/api/config/sound', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled: soundEnabled }) });
```

- [ ] **Step 4: 验证语法**

```bash
python -m py_compile E:/project/wechat_chanel/web_server.py && echo OK
```

---

### Task 4: 最小化到托盘

**Files:**
- Modify: `app.py` — 托盘生命周期

- [ ] **Step 1: 修改 app.py，添加托盘管理**

Replace `app.py` main function (lines 32-58):

```python
def main():
    from web_server import app

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

        # Generate a simple green-circle icon (32x32)
        icon_img = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
        draw = ImageDraw.Draw(icon_img)
        draw.ellipse([2, 2, 30, 30], fill=(7, 193, 96))
        draw.text((8, 6), "微", fill=(255, 255, 255))

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
                # Use native MessageBox (thread-safe, unlike pywebview GUI calls)
                import ctypes
                result = ctypes.windll.user32.MessageBoxW(0,
                    "有上传任务正在进行中，确定要退出吗？", "视频号上传", 1)  # MB_OKCANCEL
                if result != 1:  # IDOK
                    return

            icon.stop()
            window.destroy()
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

        # Override window close to minimize to tray
        original_on_closing = window.events.closing

        def on_closing():
            window.hide()
            return False  # prevent default close

        window.events.closing = on_closing

        # Run tray in thread so it doesn't block webview
        threading.Thread(target=tray_icon.run, daemon=True).start()

    except ImportError:
        pass  # tray optional, skip if deps missing
    # ---- End Tray ----

    webview.start()
```

- [ ] **Step 2: 确保 frozen 模式下图标可打包**

In `视频号上传.spec`, no change needed — pystray icon is generated at runtime with PIL.

- [ ] **Step 3: 验证语法**

```bash
python -m py_compile E:/project/wechat_chanel/app.py && echo OK
```

- [ ] **Step 4: 安装依赖并测试**

```bash
pip install pystray Pillow plyer
python E:/project/wechat_chanel/app.py
# Test: close window → should minimize to tray
# Test: tray right-click → show window
# Test: tray right-click → exit
```

---

### Task 5: 构建验证

- [ ] **Step 1: 在 spec hiddenimports 中添加 plyer**

```python
# 视频号上传.spec
hiddenimports=['accounts', 'uploader', 'logger', 'plyer', 'pystray', 'PIL'],
```

- [ ] **Step 2: 重建 EXE**

```bash
cd E:/project/wechat_chanel && python -m PyInstaller 视频号上传.spec --noconfirm
```

- [ ] **Step 3: 验证构建体积**

```bash
du -sh E:/project/wechat_chanel/dist/视频号上传
```

---

### Task 6: 提交

- [ ] **Step 1: Commit all changes**

```bash
rtk git add -A
rtk git commit -m "feat: upload retry, sound notification, system tray

- 失败后立即重试最多3次，状态和日志同步
- 全部完成时系统通知气泡 + 提示音
- 提示音开关 UI，持久化到 localStorage
- 最小化到托盘，pystray 实现"
```
