"""
视频号批量上传 - 本地 Web 管理面板 v2
Flask + 拖拽上传界面 + Playwright 后端
"""
import os
import sys
import json
import re
import asyncio
import atexit
import threading
import mimetypes
from pathlib import Path
from datetime import datetime

sys.stdout.reconfigure(encoding='utf-8') if sys.stdout else None

from flask import Flask, render_template, request, jsonify, send_file

import shutil
import uuid

import accounts as account_mgr
from uploader import WeChatUploader, CREATE_URL, SEL_ACCOUNT_INFO
from logger import get_logger

log = get_logger("server")

app = Flask(__name__)
app.config['TEMPLATES_AUTO_RELOAD'] = True

_BASE_DIR = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent

RESULTS_DIR = _BASE_DIR / "data" / "results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)
TEMP_DIR = _BASE_DIR / "data" / "temp"
TEMP_DIR.mkdir(parents=True, exist_ok=True)
_ALLOWED_DIRS = (TEMP_DIR.resolve(), _BASE_DIR.resolve())
MAX_UPLOAD_SIZE = 20 * 1024 * 1024 * 1024  # 20GB

def _clean_temp_dir():
    for f in TEMP_DIR.iterdir():
        try:
            if f.is_dir():
                shutil.rmtree(f, ignore_errors=True)
            else:
                f.unlink()
        except Exception as e:
            log.debug(f"非关键操作失败: {e}")

_clean_temp_dir()
atexit.register(_clean_temp_dir)

_account_upload_state: dict[str, dict] = {}
_state_lock = threading.Lock()

def _get_or_create_account_state(account_name: str) -> dict:
    """Get or lazily initialize upload state for an account."""
    with _state_lock:
        if account_name not in _account_upload_state:
            _account_upload_state[account_name] = {
                "running": False,
                "status": "",
                "progress": 0,
                "logs": [],
                "result": None,
                "cancelled": True,   # re-created as asyncio.Event when task starts
                "skip_current": True,
                "_task": None,
                "_video_queue": [],
                "_current_index": 0,
                "_interval_min": 0,
            }
        return _account_upload_state[account_name]

_MAX_LOG_LINES = 200

def _add_log(account_name: str, msg: str):
    state = _get_or_create_account_state(account_name)
    state["logs"].append(msg)
    if len(state["logs"]) > _MAX_LOG_LINES:
        state["logs"] = state["logs"][-_MAX_LOG_LINES:]

def _is_profile_busy(account_name: str) -> bool:
    """检查该账号的 profile 是否已被浏览器占用（上传中 / 后台打开 / 缓存中）"""
    with _state_lock:
        state = _account_upload_state.get(account_name, {})
        if state.get("running"):
            return True
        if account_name in _uploader_cache:
            return True
    return False


def _compute_combined_progress(state: dict) -> int:
    """计算总进度：已完成视频数 + 当前视频子进度 → 整体百分比"""
    uploader = state.get("_current_uploader")
    if uploader and state.get("running"):
        completed = state.get("_current_index", 0)
        total = state.get("_total_videos", 1)
        sub = getattr(uploader, "_upload_progress", 0) / 100.0
        return int((completed + sub) / max(total, 1) * 100)
    return state.get("progress", 0)


_sound_enabled = True


def _notify_upload_complete(account_name: str, total: int):
    """Upload complete: 原生 Windows 弹窗 + 系统提示音"""
    state = _get_or_create_account_state(account_name)
    queue = state.get("_video_queue", [])
    failed = sum(1 for v in queue
                 if v.get("_status") != WeChatUploader.STATUS_PUBLISHED
                 and v.get("_status") != "skipped")
    success = total - failed

    msg = f"{success} 个视频上传完成（共 {total} 个）"
    if failed > 0:
        msg += f"，{failed} 个失败"

    import ctypes, threading
    def _show():
        ctypes.windll.user32.MessageBoxW(0, msg, f"视频号上传 - {account_name}", 0x00040040)  # MB_OK|MB_ICONINFORMATION
    threading.Thread(target=_show, daemon=True).start()

    if _sound_enabled:
        try:
            import winsound
            winsound.MessageBeep(0x00000040)
        except Exception:
            pass


_scan_state = {
    "scanning": False,
    "status": "",  # loading|waiting|scanned|confirming|logged_in|expired|timeout|error|cancelled
    "qrcode": "",
    "result": None,
    "cancelled": False,
    "_page": None,
    "_uploader": None,
}

_debug_mode = False

# 持久化事件循环 + uploader 缓存，视频间复用浏览器会话
_uploader_cache = {}
_MAX_CONCURRENT = int(os.environ.get("WECHAT_MAX_CONCURRENT", "3"))
_upload_semaphore = None  # 在事件循环线程内创建，确保绑定正确
_cooldown_timers: dict[str, asyncio.Task] = {}
_COOLDOWN_SECONDS = 60
_event_loop = None

def _start_persistent_loop():
    global _event_loop, _upload_semaphore
    loop = asyncio.new_event_loop()
    _event_loop = loop
    _upload_semaphore = asyncio.Semaphore(_MAX_CONCURRENT)
    asyncio.set_event_loop(loop)
    loop.run_forever()

_event_loop_thread = threading.Thread(target=_start_persistent_loop, daemon=True)
_event_loop_thread.start()

def _close_uploader_safe(uploader):
    """在持久化事件循环上安全关闭 uploader（fire-and-forget）"""
    async def _close():
        try:
            await uploader.close()
        except Exception as e:
            log.debug(f"非关键操作失败: {e}")
    if _event_loop and not _event_loop.is_closed():
        asyncio.run_coroutine_threadsafe(_close(), _event_loop)

def _cleanup_uploaders():
    with _state_lock:
        for name, timer in list(_cooldown_timers.items()):
            timer.cancel()
        _cooldown_timers.clear()
        for name, u in list(_uploader_cache.items()):
            _close_uploader_safe(u)
        _uploader_cache.clear()

atexit.register(_cleanup_uploaders)

def _graceful_shutdown(timeout=10):
    """优雅关闭：取消所有上传任务，等待完成，关闭浏览器（同步函数，可被 app.py 调用）"""
    import time as _time

    # 1. 取消所有运行中的上传
    with _state_lock:
        states = dict(_account_upload_state)

    for name, state in states.items():
        if state.get("running"):
            cancel_ev = state.get("cancelled")
            if isinstance(cancel_ev, asyncio.Event) and _event_loop and not _event_loop.is_closed():
                try:
                    asyncio.run_coroutine_threadsafe(_async_set_event(cancel_ev), _event_loop)
                except Exception:
                    pass

    # 2. 等待上传任务结束（最多等 timeout 秒）
    deadline = _time.time() + timeout
    while _time.time() < deadline:
        with _state_lock:
            if not any(s.get("running") for s in _account_upload_state.values()):
                break
        _time.sleep(0.5)

    # 3. 关闭所有浏览器并清理锁文件
    _cleanup_uploaders()

async def _async_set_event(ev: asyncio.Event):
    ev.set()


async def _check_session_active(profile_dir: Path) -> bool:
    """快速预检：用无头浏览器探测会话是否有效。
    返回 True = 会话有效可无头上传，False = 需有头扫码登录。"""
    from uploader import WeChatUploader, SEL_ACCOUNT_INFO
    checker = WeChatUploader(profile_dir, headless=True)
    try:
        await checker.start()
        page = await checker._context.new_page()
        try:
            await page.goto(CREATE_URL, wait_until="domcontentloaded", timeout=20000)
            await page.locator(SEL_ACCOUNT_INFO).first.wait_for(
                state="attached", timeout=15000
            )
            return True  # 会话有效，可继续无头
        except Exception:
            return False  # 需要重新扫码
        finally:
            await page.close()
    except Exception:
        return False  # 探测失败，走有头模式保底
    finally:
        await checker.close()

async def _run_account_upload(account_name: str, profile_dir: Path, headless: bool):
    """Process the full video queue for one account, respecting cancel/skip/semaphore."""
    state = _get_or_create_account_state(account_name)
    state["running"] = True
    state["progress"] = 0
    state["logs"] = []
    cancel_ev = asyncio.Event()
    skip_ev = asyncio.Event()
    state["cancelled"] = cancel_ev
    state["skip_current"] = skip_ev
    state["status"] = "等待中..."

    if not profile_dir.exists():
        state["status"] = "账号 profile 目录不存在"
        state["running"] = False
        return

    uploader = None
    try:
        async with _upload_semaphore:
            if cancel_ev.is_set():
                state["status"] = "已取消"
                state["running"] = False
                return

            # --- session reuse or creation ---
            with _state_lock:
                if account_name in _uploader_cache:
                    uploader = _uploader_cache[account_name]
                    if uploader._context is None:
                        _uploader_cache.pop(account_name, None)
                        uploader = None

            if uploader is None:
                # 快速预检会话是否有效，决定是否需要可见浏览器扫码
                _effective_headless = headless
                if headless:
                    _effective_headless = await _check_session_active(profile_dir)

                uploader = WeChatUploader(profile_dir, headless=_effective_headless)
                await uploader.start()
                if cancel_ev.is_set():
                    await uploader.close()
                    state["status"] = "已取消"
                    state["running"] = False
                    return
                if not await uploader.ensure_login():
                    state["status"] = "登录失败"
                    _add_log(account_name, "登录失败，请先扫码登录")
                    await uploader.close()
                    state["running"] = False
                    return
                duplicate = None
                with _state_lock:
                    # 避免 TOCTOU：另一个并发调用可能已抢先写入
                    if account_name not in _uploader_cache:
                        _uploader_cache[account_name] = uploader
                    else:
                        duplicate = uploader
                        uploader = _uploader_cache[account_name]
                if duplicate:
                    await duplicate.close()

            # --- process queue ---
            queue = state["_video_queue"]
            total = len(queue)
            state["_total_videos"] = total
            interval = state.get("_interval_min", 0)

            for i in range(total):
                if cancel_ev.is_set():
                    state["status"] = "已取消"
                    break

                # Re-read current item (queue may have been edited externally)
                video = state["_video_queue"][i]
                state["_current_index"] = i
                title = video.get("title", "")
                state["status"] = f"上传中 ({i+1}/{total})"
                _add_log(account_name, f"[开始] {title}")

                state["_current_uploader"] = uploader
                max_retries = 3
                result = {"status": WeChatUploader.STATUS_UNKNOWN}
                for retry in range(max_retries + 1):
                    if cancel_ev.is_set() or skip_ev.is_set():
                        break

                    uploader._upload_progress = 0

                    if retry > 0:
                        state["status"] = f"重试中 ({retry}/{max_retries})…"
                        _add_log(account_name, f"[重试] {title} ({retry}/{max_retries})")

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

                if interval > 0 and i < total - 1 and not cancel_ev.is_set():
                    state["status"] = f"等待间隔 {interval} 分钟..."
                    await asyncio.sleep(interval * 60)

            if not cancel_ev.is_set():
                state["status"] = "全部完成"
                state["progress"] = 100
                _notify_upload_complete(account_name, total)

    except Exception as e:
        state["status"] = f"异常: {e}"
        _add_log(account_name, f"[异常] {e}")
        log.error(f"上传异常 [{account_name}]: {e}", exc_info=True)
    finally:
        state["running"] = False
        state["_task"] = None
        state["_current_uploader"] = None
        # Start cooldown timer: close browser after idle timeout
        with _state_lock:
            if uploader and account_name not in _cooldown_timers:
                async def _cooldown():
                    await asyncio.sleep(_COOLDOWN_SECONDS)
                    with _state_lock:
                        if account_name in _cooldown_timers:
                            del _cooldown_timers[account_name]
                        cached = _uploader_cache.pop(account_name, None)
                    if cached:
                        try:
                            await cached.close()
                        except Exception:
                            pass
                        log.info(f"[cooldown] 已关闭 {account_name} 浏览器会话")
                loop = asyncio.get_event_loop()
                t = loop.create_task(_cooldown())
                _cooldown_timers[account_name] = t

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/debug", methods=["GET"])
def api_debug_get():
    return jsonify({"debug": _debug_mode})

@app.route("/api/debug", methods=["POST"])
def api_debug_set():
    global _debug_mode
    data = request.get_json()
    _debug_mode = bool(data.get("debug", False))
    return jsonify({"debug": _debug_mode})

@app.route("/api/config/sound", methods=["GET"])
def api_sound_get():
    return jsonify({"enabled": _sound_enabled})


@app.route("/api/config/sound", methods=["POST"])
def api_sound_set():
    global _sound_enabled
    data = request.get_json()
    _sound_enabled = bool(data.get("enabled", True))
    return jsonify({"enabled": _sound_enabled})


@app.route("/api/accounts", methods=["GET"])
def api_list_accounts():
    return jsonify({"accounts": account_mgr.list_accounts()})

@app.route("/api/accounts/<name>", methods=["DELETE"])
def api_remove_account(name):
    return jsonify(account_mgr.remove_account(name))

_check_state = {"checking": False, "results": {}, "done": False}

@app.route("/api/accounts/check", methods=["POST"])
def api_check_accounts():
    """启动后台检测所有已登录账号的有效性"""
    global _check_state
    if _check_state["checking"]:
        return jsonify({"error": "检测进行中"}), 409

    accounts_to_check = [a for a in account_mgr.list_accounts() if a.get("last_login")]
    if not accounts_to_check:
        return jsonify({"message": "无已登录账号"})

    _check_state = {"checking": True, "results": {}, "done": False}

    def do_check():
        async def _check():
            global _check_state
            for acct in accounts_to_check:
                name = acct["name"]
                # 跳过正在上传/使用中的账号，避免 profile 冲突
                if _is_profile_busy(name):
                    continue  # 不打扰也不标记，保持上次的检测结果
                profile_dir = Path(acct["profile_dir"])
                if not profile_dir.exists():
                    _check_state["results"][name] = False
                    account_mgr.clear_last_login(name)
                    continue
                try:
                    valid = await _check_session_active(profile_dir)
                    _check_state["results"][name] = valid
                except Exception as e:
                    log.debug(f"非关键操作失败: {e}")
                    _check_state["results"][name] = False
                if not _check_state["results"][name]:
                    account_mgr.clear_last_login(name)
            _check_state["checking"] = False
            _check_state["done"] = True
        asyncio.run(_check())

    threading.Thread(target=do_check, daemon=True).start()
    return jsonify({"message": "检测已启动"}), 202

@app.route("/api/accounts/check/status", methods=["GET"])
def api_check_status():
    return jsonify(_check_state)

@app.route("/api/accounts/<name>/dashboard", methods=["POST"])
def api_open_dashboard(name):
    """用同一 profile 在 Chrome 窗口中打开视频号后台"""
    profile_dir = account_mgr.get_profile_dir(name)
    if not profile_dir:
        return jsonify({"error": f"账号 {name} 不存在"}), 404

    if _is_profile_busy(name):
        return jsonify({"error": f"账号 {name} 正在上传中，请等待完成后再打开后台"}), 409


    def do_open():
        async def _open():
            uploader = WeChatUploader(profile_dir, headless=False)
            try:
                await uploader.start()
                page = await uploader._context.new_page()
                await page.goto(CREATE_URL, wait_until="domcontentloaded", timeout=60000)
                # 等待 SPA 页面 JS 渲染完成 — 等关键元素出现
                try:
                    await page.locator(SEL_ACCOUNT_INFO).wait_for(state="visible", timeout=30000)
                except Exception:
                    pass
                try:
                    await page.locator(SEL_UPLOAD_ZONE).wait_for(state="attached", timeout=30000)
                except Exception:
                    pass
                # 不调用 uploader.close()，窗口保持打开供用户操作
            except Exception as e:
                log.error(f"打开后台失败 [{name}]: {e}", exc_info=True)
        asyncio.run(_open())

    threading.Thread(target=do_open, daemon=True).start()
    return jsonify({"message": "已打开后台"})

@app.route("/api/accounts/<name>/login", methods=["POST"])
def api_login_account(name):
    profile_dir = account_mgr.get_profile_dir(name)
    if not profile_dir:
        return jsonify({"error": f"账号 {name} 不存在"}), 404

    if _is_profile_busy(name):
        return jsonify({"error": f"账号 {name} 正在上传中，请等待完成后再登录"}), 409


    def do_login():
        async def _login():
            uploader = WeChatUploader(profile_dir, headless=False)
            try:
                await uploader.start()
                success = await uploader.ensure_login(timeout_seconds=120)
                if not success:
                    account_mgr.clear_last_login(name)
                    return
                # 校验重新登录的账号与已记录账号是否一致
                acct = account_mgr.get_account(name)
                if acct:
                    page = await uploader._context.new_page()
                    await page.goto(CREATE_URL, wait_until="domcontentloaded", timeout=30000)
                    try:
                        await page.locator(SEL_ACCOUNT_INFO).first.wait_for(state="visible", timeout=15000)
                        nickname = await uploader.scrape_nickname(page)
                        await page.close()
                        recorded = (acct.get("nickname") or "").strip()
                        current = (nickname or "").strip()
                        if recorded and current and recorded != current:
                            log.warning(f"[登录] 账号名不匹配: 已记录={recorded}, 当前={current}")
                            account_mgr.clear_last_login(name)
                            return
                    except Exception as e:
                        log.warning(f"[登录] 昵称校验失败: {e}")
                        await page.close() if page else None
                account_mgr.update_last_login(name)
            except Exception as e:
                log.error(f"登录失败 [{name}]: {e}", exc_info=True)
            finally:
                await uploader.close()
        asyncio.run(_login())

    threading.Thread(target=do_login, daemon=True).start()
    return jsonify({"message": "网页打开中，请稍等"})

@app.route("/api/accounts/<name>/upload/start", methods=["POST"])
def api_account_upload_start(name):
    """Start batch upload for a specific account's queue."""
    profile_dir = account_mgr.get_profile_dir(name)
    if not profile_dir:
        return jsonify({"error": f"账号 {name} 不存在"}), 404

    state = _get_or_create_account_state(name)
    if state["running"]:
        return jsonify({"error": f"账号 {name} 正在上传中"}), 409

    data = request.get_json()
    if not data:
        return jsonify({"error": "请求体为空"}), 400

    videos = data.get("videos", [])
    if not videos:
        return jsonify({"error": "视频队列为空"}), 400

    for v in videos:
        if not Path(v.get("video_path", "")).exists():
            return jsonify({"error": f"视频文件不存在: {v.get('video_path')}"}), 400

    # Populate state
    state["_video_queue"] = videos
    state["_interval_min"] = data.get("interval_min", 0)
    state["progress"] = 0
    state["logs"] = []
    state["_current_index"] = 0
    state["_total_videos"] = len(videos)

    # Cancel any cooldown timer
    with _state_lock:
        if name in _cooldown_timers:
            _cooldown_timers[name].cancel()
            del _cooldown_timers[name]

    # Launch upload task on persistent event loop
    headless = not _debug_mode
    coro = _run_account_upload(name, Path(profile_dir), headless)
    task = asyncio.run_coroutine_threadsafe(coro, _event_loop)
    state["_task"] = task

    return jsonify({"message": f"账号 {name} 上传已启动"})

@app.route("/api/accounts/<name>/upload/status", methods=["GET"])
def api_account_upload_status(name):
    """Get upload status for a specific account."""
    state = _get_or_create_account_state(name)
    cancelled_is_set = False
    try:
        ev = state.get("cancelled")
        if ev is True:
            cancelled_is_set = True
        elif isinstance(ev, asyncio.Event):
            cancelled_is_set = ev.is_set()
    except Exception as e:
        log.debug(f"非关键操作失败: {e}")
    return jsonify({
        "running": state.get("running", False),
        "status": state.get("status", ""),
        "progress": _compute_combined_progress(state),
        "logs": state.get("logs", [])[-20:],
        "result": state.get("result"),
        "cancelled": cancelled_is_set,
        "queue": [
            {"title": v.get("title", ""), "name": Path(v.get("video_path", "")).name,
             "status": v.get("_status", "")}
            for v in state.get("_video_queue", [])
        ],
        "current_index": state.get("_current_index", 0),
    })

@app.route("/api/accounts/<name>/upload/cancel", methods=["POST"])
def api_account_upload_cancel(name):
    """Cancel upload for a specific account."""
    state = _get_or_create_account_state(name)
    if not state["running"]:
        return jsonify({"message": "该账号无进行中的上传"})

    log.info(f"取消上传 [{name}]")
    cancel_ev = state.get("cancelled")
    if isinstance(cancel_ev, asyncio.Event):
        asyncio.run_coroutine_threadsafe(_async_set_event(cancel_ev), _event_loop)

    skip_ev = state.get("skip_current")
    if isinstance(skip_ev, asyncio.Event):
        asyncio.run_coroutine_threadsafe(_async_set_event(skip_ev), _event_loop)

    with _state_lock:
        uploader = _uploader_cache.pop(name, None)
    if uploader:
        _close_uploader_safe(uploader)

    state["status"] = "正在取消..."
    return jsonify({"message": "取消指令已发送"})

@app.route("/api/accounts/<name>/upload/skip", methods=["POST"])
def api_account_upload_skip(name):
    """Skip the current video for an account."""
    state = _get_or_create_account_state(name)
    if not state["running"]:
        return jsonify({"error": "该账号无进行中的上传"}), 400

    skip_ev = state.get("skip_current")
    if isinstance(skip_ev, asyncio.Event):
        asyncio.run_coroutine_threadsafe(_async_set_event(skip_ev), _event_loop)
        return jsonify({"message": "已跳过当前视频"})
    return jsonify({"error": "跳过信号不可用"})

@app.route("/api/upload/status/all", methods=["GET"])
def api_all_upload_status():
    """Return summary status for all accounts with active state."""
    result = {}
    for n, s in _account_upload_state.items():
        cancelled_is_set = False
        ev = s.get("cancelled")
        if ev is True or (isinstance(ev, asyncio.Event) and ev.is_set()):
            cancelled_is_set = True
        result[n] = {
            "running": s.get("running", False),
            "status": s.get("status", ""),
            "progress": _compute_combined_progress(s),
            "cancelled": cancelled_is_set,
        }
    return jsonify(result)

@app.route("/api/accounts/add-with-scan", methods=["POST"])
def api_add_account_with_scan():
    """扫码添加账号：后台无头浏览器 → 截取二维码 → 前端展示 → 自动完成登录"""
    global _scan_state

    if _scan_state["scanning"]:
        return jsonify({"error": "已有扫码进行中"}), 409

    _scan_state["scanning"] = True
    _scan_state["status"] = "loading"
    _scan_state["qrcode"] = ""
    _scan_state["result"] = None
    _scan_state["cancelled"] = False
    _scan_state["_page"] = None
    _scan_state["_uploader"] = None

    def do_scan():
        async def _scan():
            global _scan_state
            temp_name = f"scan_{datetime.now().strftime('%Y%m%d%H%M%S')}"
            profile_dir = account_mgr.ACCOUNTS_ROOT / temp_name / "profile"
            uploader = WeChatUploader(profile_dir, headless=True)
            _scan_state["_uploader"] = uploader

            page = None
            try:
                await uploader.start()
                page = await uploader._context.new_page()
                _scan_state["_page"] = page

                await page.goto(CREATE_URL, wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(3000)

                # 已有登录 session → 直接抓昵称创建账号
                if await page.locator(SEL_ACCOUNT_INFO).count() > 0:
                    log.info("[QR] 已有登录 session，直接获取昵称")
                    nickname = await uploader.scrape_nickname(page)
                    safe_name = _sanitize_nickname(nickname)
                    result = account_mgr.add_account(safe_name, nickname)
                    account_mgr.update_last_login(safe_name)
                    _scan_state["status"] = "logged_in"
                    _scan_state["result"] = {
                        "success": True,
                        "account": result.get("account", {"name": safe_name, "nickname": nickname}),
                    }
                    _scan_state["scanning"] = False
                    return

                # 截取二维码（重试等待 iframe 加载）
                qrcode = None
                for attempt in range(10):
                    try:
                        qrcode = await uploader.capture_qrcode(page)
                        break
                    except Exception as e:
                        log.warning(f"二维码获取失败: {e}")
                        await page.wait_for_timeout(1000)
                if not qrcode:
                    _scan_state["result"] = {"error": "无法获取二维码"}
                    _scan_state["scanning"] = False
                    return

                _scan_state["qrcode"] = qrcode
                _scan_state["status"] = "waiting"

                # 轮询等待扫码（180s 超时，0.5s 间隔）
                for i in range(360):
                    # 检测取消
                    if _scan_state["cancelled"]:
                        log.info("[QR] 用户取消扫码")
                        _scan_state["status"] = "cancelled"
                        _scan_state["result"] = {"error": "用户取消"}
                        _scan_state["scanning"] = False
                        return

                    # 检测登录成功
                    if await page.locator(SEL_ACCOUNT_INFO).count() > 0:
                        log.info("[QR] 登录成功！")
                        _scan_state["status"] = "logged_in"
                        break

                    # 检测二维码过期 → 刷新
                    try:
                        if await uploader.check_qrcode_expired(page):
                            log.info("[QR] 二维码过期，刷新中...")
                            _scan_state["status"] = "expired"
                            await page.reload(wait_until="domcontentloaded")
                            await page.wait_for_timeout(3000)
                            for attempt2 in range(10):
                                try:
                                    qrcode = await uploader.capture_qrcode(page)
                                    break
                                except Exception as e:
                                    log.warning(f"二维码获取失败: {e}")
                                    await page.wait_for_timeout(1000)
                            _scan_state["qrcode"] = qrcode
                            _scan_state["status"] = "waiting"
                            continue
                    except Exception as e:
                        log.debug(f"非关键操作失败: {e}")
                        pass

                    # 检测扫码状态
                    try:
                        scan_status = await uploader.check_qrcode_scanned(page)
                        if scan_status in ("scanned", "confirming"):
                            _scan_state["status"] = scan_status
                    except Exception as e:
                        log.debug(f"非关键操作失败: {e}")
                        pass

                    await asyncio.sleep(0.5)
                else:
                    _scan_state["status"] = "timeout"
                    _scan_state["result"] = {"error": "登录超时"}
                    _scan_state["scanning"] = False
                    return

                # 登录成功 → 抓昵称 → 创建账号 → 保留 session
                nickname = await uploader.scrape_nickname(page)
                await uploader.close()
                _scan_state["_uploader"] = None
                _scan_state["_page"] = None

                safe_name = _sanitize_nickname(nickname)
                result = account_mgr.add_account(safe_name, nickname)
                account_mgr.update_last_login(safe_name)

                # 将 temp profile 数据移到账号的真实 profile 目录（保留 session）
                target_profile = Path(result["account"]["profile_dir"])
                if target_profile.exists():
                    shutil.rmtree(target_profile, ignore_errors=True)
                shutil.move(str(profile_dir), str(target_profile))
                shutil.rmtree(str(profile_dir.parent), ignore_errors=True)

                _scan_state["result"] = {
                    "success": True,
                    "account": result.get("account", {"name": safe_name, "nickname": nickname}),
                }
            except Exception as e:
                import traceback as _tb
                _full_tb = _tb.format_exc()
                log.error(f"扫码添加账号失败: {e}\n{_full_tb}")
                _scan_state["result"] = {"error": str(e), "traceback": _full_tb}
            finally:
                _scan_state["scanning"] = False
                if page:
                    try:
                        await page.close()
                    except Exception as e:
                        log.debug(f"非关键操作失败: {e}")
                        pass
                if _scan_state["_uploader"]:
                    try:
                        await _scan_state["_uploader"].close()
                    except Exception as e:
                        log.debug(f"非关键操作失败: {e}")
                        pass
                _scan_state["_uploader"] = None
                _scan_state["_page"] = None
                if profile_dir.exists():
                    shutil.rmtree(str(profile_dir.parent), ignore_errors=True)

        asyncio.run(_scan())

    threading.Thread(target=do_scan, daemon=True).start()
    return jsonify({"message": "正在获取二维码..."}), 202


@app.route("/api/accounts/add-with-scan/cancel", methods=["POST"])
def api_cancel_scan():
    """取消当前扫码"""
    global _scan_state
    _scan_state["cancelled"] = True
    return jsonify({"message": "已取消"})

@app.route("/api/accounts/add-with-scan/status", methods=["GET"])
def api_scan_status():
    """查询扫码添加的状态，返回二维码和状态"""
    return jsonify({
        "scanning": _scan_state["scanning"],
        "status": _scan_state["status"],
        "qrcode": _scan_state["qrcode"],
        "result": _scan_state["result"],
    })


def _sanitize_nickname(nickname: str) -> str:
    cleaned = re.sub(r'[^\u3400-\u9fff\u3000-\u303fa-zA-Z0-9_\-]', '', nickname)
    if not cleaned:
        cleaned = f"user_{datetime.now().strftime('%H%M%S')}"
    return cleaned[:30]


@app.route("/api/preview/<path:filepath>")
def api_preview(filepath):
    """预览媒体文件（返回文件内容）"""
    resolved = Path(filepath).resolve()
    if not any(resolved.is_relative_to(d) for d in _ALLOWED_DIRS):
        return "Forbidden", 403
    if not resolved.is_file():
        return "Not found", 404
    mime, _ = mimetypes.guess_type(str(resolved))
    return send_file(str(resolved), mimetype=mime or "application/octet-stream")


@app.route("/api/upload-temp", methods=["POST"])
def api_upload_temp():
    """接收浏览器上传的文件，存到临时目录，返回本地路径"""
    if "file" not in request.files:
        return jsonify({"error": "无文件"}), 400
    content_length = request.content_length
    if content_length and content_length > MAX_UPLOAD_SIZE:
        return jsonify({"error": "文件过大，上限 20GB"}), 413

    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "空文件名"}), 400
    safe_name = Path(f.filename).name
    dest = TEMP_DIR / f"{uuid.uuid4().hex[:8]}_{safe_name}"
    f.save(str(dest))
    return jsonify({"path": str(dest), "name": f.filename})

# ===== 窗口控制（由 app.py 注入 _main_window / _tray_exit） =====
_main_window = None
_tray_exit = None

@app.route("/api/window/show", methods=["POST"])
def api_window_show():
    """将窗口提到最前"""
    if _main_window:
        try:
            _main_window.show()
        except Exception:
            pass
    return jsonify({"ok": True})

@app.route("/api/window/minimize", methods=["POST"])
def api_window_minimize():
    """隐藏窗口到托盘"""
    if _main_window:
        try:
            _main_window.hide()
        except Exception:
            pass
    return jsonify({"ok": True})

@app.route("/api/window/exit", methods=["POST"])
def api_window_exit():
    """退出程序"""
    if _tray_exit:
        _tray_exit()
    return jsonify({"ok": True})

def main():
    import argparse
    parser = argparse.ArgumentParser(description="视频号批量上传 Web 面板")
    parser.add_argument("--port", type=int, default=5050, help="端口 (默认 5050)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    log.info(f"视频号上传面板: http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=args.debug)

if __name__ == "__main__":
    main()
