"""
视频号批量上传 - 本地 Web 管理面板 v2
Flask + 拖拽上传界面 + Playwright 后端
"""
import sys
import json
import csv
import re
import asyncio
import atexit
import threading
import mimetypes
from pathlib import Path
from datetime import datetime

sys.stdout.reconfigure(encoding='utf-8') if sys.stdout else None

from flask import Flask, render_template, request, jsonify, send_file, url_for

import shutil

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
            f.unlink()
        except Exception:
            pass

_clean_temp_dir()
atexit.register(_clean_temp_dir)

_upload_state = {
    "running": False,
    "status": "",
    "logs": [],
    "result": None,
    "cancelled": False,
    "progress": 0,
    "_uploader": None,
}

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
_event_loop = None

def _start_persistent_loop():
    global _event_loop
    loop = asyncio.new_event_loop()
    _event_loop = loop
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
    for _, u in _uploader_cache.items():
        _close_uploader_safe(u)
    _uploader_cache.clear()

atexit.register(_cleanup_uploaders)

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
                profile_dir = Path(acct["profile_dir"])
                if not profile_dir.exists():
                    _check_state["results"][name] = False
                    continue
                try:
                    uploader = WeChatUploader(profile_dir, headless=True)
                    await uploader.start()
                    valid = await uploader.ensure_login(timeout_seconds=30)
                    await uploader.close()
                    _check_state["results"][name] = valid
                except Exception:
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

    def do_open():
        async def _open():
            uploader = WeChatUploader(profile_dir, headless=False)
            try:
                await uploader.start()
                page = await uploader._context.new_page()
                await page.goto(CREATE_URL, wait_until="domcontentloaded", timeout=30000)
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

    def do_login():
        async def _login():
            uploader = WeChatUploader(profile_dir, headless=False)
            try:
                await uploader.start()
                success = await uploader.ensure_login(timeout_seconds=180)
                if success:
                    account_mgr.update_last_login(name)
                else:
                    account_mgr.clear_last_login(name)
            except Exception as e:
                log.error(f"登录失败 [{name}]: {e}", exc_info=True)
            finally:
                await uploader.close()
        asyncio.run(_login())

    threading.Thread(target=do_login, daemon=True).start()
    return jsonify({"message": "网页打开中，请稍等"})

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

                await page.goto(CREATE_URL, wait_until="domcontentloaded", timeout=30000)
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
                    except Exception:
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
                                except Exception:
                                    await page.wait_for_timeout(1000)
                            _scan_state["qrcode"] = qrcode
                            _scan_state["status"] = "waiting"
                            continue
                    except Exception:
                        pass

                    # 检测扫码状态
                    try:
                        scan_status = await uploader.check_qrcode_scanned(page)
                        if scan_status in ("scanned", "confirming"):
                            _scan_state["status"] = scan_status
                    except Exception:
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
                log.error(f"扫码添加账号失败: {e}", exc_info=True)
                _scan_state["result"] = {"error": str(e)}
            finally:
                _scan_state["scanning"] = False
                if page:
                    try:
                        await page.close()
                    except Exception:
                        pass
                if _scan_state["_uploader"]:
                    try:
                        await _scan_state["_uploader"].close()
                    except Exception:
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
    cleaned = re.sub(r'[^一-龥a-zA-Z0-9_\-]', '', nickname)
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
    dest = TEMP_DIR / safe_name
    f.save(str(dest))
    return jsonify({"path": str(dest), "name": f.filename})

@app.route("/api/upload", methods=["POST"])
def api_start_upload():
    """接收表单数据，启动单条上传"""
    global _upload_state

    data = request.get_json()
    account_name = data.get("account", "").strip()
    video_path = data.get("video_path", "").strip()
    title = data.get("title", "").strip()
    description = data.get("description", "").strip()
    cover_path = data.get("cover_path", "").strip() or ""
    short_drama_name = data.get("short_drama_name", "").strip()
    publish_time = data.get("publish_time", "").strip()
    location = data.get("location", "none")

    if _upload_state.get("running"):
        return jsonify({"error": "上传进行中"}), 409

    if not account_name:
        return jsonify({"error": "请选择账号"}), 400
    if not video_path:
        return jsonify({"error": "请选择视频"}), 400
    if not Path(video_path).exists():
        return jsonify({"error": f"视频文件不存在: {video_path}"}), 400

    profile_dir = account_mgr.get_profile_dir(account_name)
    if not profile_dir:
        return jsonify({"error": f"账号 {account_name} 不存在"}), 400

    _upload_state = {
        "running": True,
        "status": "开始上传...",
        "logs": [],
        "result": None,
        "cancelled": False,
        "progress": 0,
        "_uploader": None,
        "_account_name": account_name,
    }

    async def _upload():
        global _upload_state
        sync_task = None
        _sync_done = False
        uploader = None

        try:
            if account_name in _uploader_cache:
                uploader = _uploader_cache[account_name]
                log.info(f"[缓存] 复用账号 {account_name} 的浏览器会话")
                if uploader._context is None:
                    log.info("[缓存] 浏览器已关闭，重新创建")
                    _uploader_cache.pop(account_name, None)
                    uploader = None

            if uploader is None:
                uploader = WeChatUploader(profile_dir, headless=not _debug_mode)
                _upload_state["_uploader"] = uploader
                await uploader.start()

                if _upload_state.get("cancelled"):
                    _upload_state["running"] = False
                    _upload_state["status"] = "已取消"
                    return

                if not await uploader.ensure_login():
                    _upload_state["running"] = False
                    _upload_state["status"] = "登录失败"
                    _upload_state["logs"].append("登录失败，请先扫码登录")
                    await uploader.close()
                    return

                _uploader_cache[account_name] = uploader
            else:
                _upload_state["_uploader"] = uploader

            start_at = data.get("start_at", "").strip()
            if start_at:
                try:
                    target_dt = datetime.fromisoformat(start_at)
                    wait_seconds = (target_dt - datetime.now()).total_seconds()
                    if wait_seconds > 0:
                        _upload_state["status"] = "等待定时开始..."
                        log.info(f"[定时] 等待 {wait_seconds:.0f} 秒")
                        await asyncio.sleep(wait_seconds)
                except Exception as e:
                    log.warning(f"[定时] 解析失败: {e}")

            if _upload_state.get("cancelled"):
                _upload_state["running"] = False
                _upload_state["status"] = "已取消"
                return

            _upload_state["status"] = "上传中..."
            _upload_state["logs"].append(f"[开始] {title}")

            # 后台协程：每500ms同步 WeChat 原生进度条到 _upload_state
            _sync_done = False
            async def _sync_progress():
                while not _sync_done:
                    p = uploader._upload_progress
                    if _upload_state["progress"] != p:
                        _upload_state["progress"] = p
                    await asyncio.sleep(0.5)
            sync_task = asyncio.ensure_future(_sync_progress())

            result = await uploader.upload_single(
                video_path=video_path,
                title=title,
                description=description,
                cover_path=cover_path,
                short_drama_name=short_drama_name,
                publish_time=publish_time,
                location=location,
            )
            _upload_state["progress"] = 100
            _upload_state["result"] = result
            _upload_state["running"] = False
            if result["status"] == "published":
                _upload_state["status"] = "发表成功"
                _upload_state["logs"].append(f"[成功] {title}")
            elif result["status"] == "failed":
                _upload_state["status"] = f"失败: {result.get('error', '')}"
                _upload_state["logs"].append(f"[失败] {title}: {result.get('error', '')}")
            else:
                _upload_state["status"] = "发表状态不确定"
                _upload_state["logs"].append(f"[不确定] {title}")

            # 保存结果
            result_path = RESULTS_DIR / f"{account_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            with open(result_path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.DictWriter(f, fieldnames=["video_path", "title", "status", "error"])
                w.writeheader()
                w.writerow(result)
        except Exception as e:
            _upload_state["running"] = False
            _upload_state["status"] = f"异常: {e}"
            _upload_state["logs"].append(f"[异常] {title}: {e}")
            log.error(f"上传异常 [{title}]: {e}", exc_info=True)
        finally:
            try:
                _sync_done = True
                if sync_task is not None:
                    await sync_task
            except Exception:
                pass

            if uploader is not None:
                is_success = _upload_state.get("result", {}).get("status") == "published"
                if is_success:
                    log.info(f"[缓存] 保留账号 {account_name} 的浏览器会话")
                else:
                    _uploader_cache.pop(account_name, None)
                    await uploader.close()
                    _upload_state["_uploader"] = None

    asyncio.run_coroutine_threadsafe(_upload(), _event_loop)
    return jsonify({"message": "上传任务已启动"})

@app.route("/api/upload/status", methods=["GET"])
def api_upload_status():
    return jsonify({
        "running": _upload_state["running"],
        "status": _upload_state["status"],
        "logs": _upload_state["logs"][-20:],
        "result": _upload_state["result"],
        "cancelled": _upload_state["cancelled"],
        "progress": _upload_state["progress"],
    })

@app.route("/api/upload/cancel", methods=["POST"])
def api_cancel_upload():
    """取消当前上传，关闭浏览器"""
    global _upload_state
    log.info("收到取消上传请求")
    _upload_state["cancelled"] = True

    account_name = _upload_state.get("_account_name")
    uploader = _uploader_cache.pop(account_name, None) if account_name else None
    if uploader:
        _close_uploader_safe(uploader)
        log.info(f"已关闭 {account_name} 的浏览器会话")
    elif _upload_state.get("_uploader"):
        _close_uploader_safe(_upload_state["_uploader"])
    else:
        log.warning("取消时无 uploader 引用")

    _upload_state["running"] = False
    _upload_state["status"] = "已取消"
    _upload_state["_uploader"] = None
    return jsonify({"message": "已取消"})

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
