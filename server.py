"""
视频号批量上传 - 本地 Web 管理面板 v2
Flask + 拖拽上传界面 + Playwright 后端
"""
import sys
import json
import csv
import re
import asyncio
import threading
import mimetypes
from pathlib import Path
from datetime import datetime

sys.stdout.reconfigure(encoding='utf-8') if sys.stdout else None

from flask import Flask, render_template, request, jsonify, send_file, url_for

import shutil

import accounts as account_mgr
from uploader import WeChatUploader, CREATE_URL

app = Flask(__name__)
app.config['TEMPLATES_AUTO_RELOAD'] = True

VIDEO_DIRS = []

def _get_default_browse_dirs() -> list:
    home = Path.home()
    dirs = []
    for name in ["Desktop", "Videos", "Documents"]:
        p = home / name
        if p.exists():
            dirs.append(str(p))
    for letter in "DEFGHIJK":
        p = Path(f"{letter}:\\")
        if p.exists():
            dirs.append(str(p))
    return dirs
MEDIA_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".jpg", ".jpeg", ".png", ".webp"}

_BASE_DIR = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent

RESULTS_DIR = _BASE_DIR / "data" / "results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)
TEMP_DIR = _BASE_DIR / "data" / "temp"
TEMP_DIR.mkdir(parents=True, exist_ok=True)

_upload_state = {
    "running": False,
    "status": "",
    "logs": [],
    "result": None,
}

_scan_state = {
    "scanning": False,
    "result": None,
}

_debug_mode = False

# ==================== 页面 ====================

@app.route("/")
def index():
    return render_template("index.html")

# ==================== Debug ====================

@app.route("/api/debug", methods=["GET"])
def api_debug_get():
    return jsonify({"debug": _debug_mode})

@app.route("/api/debug", methods=["POST"])
def api_debug_set():
    global _debug_mode
    data = request.get_json()
    _debug_mode = bool(data.get("debug", False))
    return jsonify({"debug": _debug_mode})

# ==================== 账号 API ====================

@app.route("/api/accounts", methods=["GET"])
def api_list_accounts():
    return jsonify({"accounts": account_mgr.list_accounts()})

@app.route("/api/accounts", methods=["POST"])
def api_add_account():
    data = request.get_json()
    name = data.get("name", "").strip()
    nickname = data.get("nickname", "").strip()
    if not name:
        return jsonify({"error": "账号名称不能为空"}), 400
    result = account_mgr.add_account(name, nickname)
    if "error" in result:
        return jsonify(result), 400
    return jsonify(result)

@app.route("/api/accounts/<name>", methods=["DELETE"])
def api_remove_account(name):
    return jsonify(account_mgr.remove_account(name))

@app.route("/api/accounts/<name>/login", methods=["POST"])
def api_login_account(name):
    profile_dir = account_mgr.get_profile_dir(name)
    if not profile_dir:
        return jsonify({"error": f"账号 {name} 不存在"}), 404

    def do_login():
        async def _login():
            uploader = WeChatUploader(profile_dir, headless=False)
            await uploader.start()
            success = await uploader.ensure_login(timeout_seconds=180)
            if success:
                account_mgr.update_last_login(name)
        asyncio.run(_login())

    threading.Thread(target=do_login, daemon=True).start()
    return jsonify({"message": "浏览器已打开，请扫码登录"})

# ==================== 扫码添加账号 API ====================

@app.route("/api/accounts/add-with-scan", methods=["POST"])
def api_add_account_with_scan():
    """扫码添加账号：打开浏览器 → 用户扫码 → 自动抓取昵称 → 创建账号"""
    global _scan_state

    if _scan_state["scanning"]:
        return jsonify({"error": "已有扫码进行中"}), 409

    _scan_state["scanning"] = True
    _scan_state["result"] = None

    def do_scan():
        async def _scan():
            global _scan_state
            temp_name = f"scan_{datetime.now().strftime('%Y%m%d%H%M%S')}"
            profile_dir = account_mgr.ACCOUNTS_ROOT / temp_name / "profile"
            uploader = WeChatUploader(profile_dir, headless=False)
            await uploader.start()

            try:
                success = await uploader.ensure_login(timeout_seconds=180)
                if not success:
                    _scan_state["scanning"] = False
                    _scan_state["result"] = {"error": "登录超时"}
                    await uploader.close()
                    return

                page = await uploader._context.new_page()
                await page.goto(CREATE_URL, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(3000)

                nickname = await uploader.scrape_nickname(page)
                await page.close()

                safe_name = _sanitize_nickname(nickname)
                result = account_mgr.add_account(safe_name, nickname)
                account_mgr.update_last_login(safe_name)

                _scan_state["result"] = {
                    "success": True,
                    "account": result.get("account", {"name": safe_name, "nickname": nickname}),
                }
            except Exception as e:
                _scan_state["result"] = {"error": str(e)}
            finally:
                _scan_state["scanning"] = False
                await uploader.close()
                shutil.rmtree(profile_dir, ignore_errors=True)

        asyncio.run(_scan())

    threading.Thread(target=do_scan, daemon=True).start()
    return jsonify({"message": "浏览器已打开，请扫码登录"}), 202


@app.route("/api/accounts/add-with-scan/status", methods=["GET"])
def api_scan_status():
    """查询扫码添加的状态"""
    return jsonify(_scan_state)


def _sanitize_nickname(nickname: str) -> str:
    cleaned = re.sub(r'[^一-龥a-zA-Z0-9_\-]', '', nickname)
    if not cleaned:
        cleaned = f"user_{datetime.now().strftime('%H%M%S')}"
    return cleaned[:30]


# ==================== 文件浏览 API ====================

@app.route("/api/browse")
def api_browse():
    """浏览视频目录，返回文件列表"""
    dir_path = request.args.get("dir", "").strip()
    if not dir_path:
        sources = VIDEO_DIRS if VIDEO_DIRS else _get_default_browse_dirs()
        result = []
        for d in sources:
            p = Path(d)
            if p.exists():
                result.append({
                    "path": str(p),
                    "name": p.name,
                    "type": "dir",
                })
        return jsonify({"files": result, "parent": None})

    target = Path(dir_path)
    if not target.exists():
        return jsonify({"error": "目录不存在"}), 404

    result = []
    try:
        for item in sorted(target.iterdir()):
            if item.name.startswith("."):
                continue
            is_dir = item.is_dir()
            if not is_dir and item.suffix.lower() not in MEDIA_EXTS:
                continue
            result.append({
                "path": str(item),
                "name": item.name,
                "type": "dir" if is_dir else item.suffix.lower(),
                "size": item.stat().st_size if not is_dir else 0,
            })
    except PermissionError:
        return jsonify({"error": "无权限访问"}), 403

    parent = str(target.parent) if str(target) != target.drive else None
    return jsonify({"files": result, "parent": parent})


@app.route("/api/preview/<path:filepath>")
def api_preview(filepath):
    """预览媒体文件（返回文件内容）"""
    f = Path(filepath)
    if not f.exists() or not f.is_file():
        return "Not found", 404
    mime, _ = mimetypes.guess_type(str(f))
    return send_file(str(f), mimetype=mime or "application/octet-stream")


@app.route("/api/finder")
def api_finder():
    """文件查找器：搜索匹配的视频/图片"""
    q = request.args.get("q", "").strip().lower()
    if not q or len(q) < 2:
        return jsonify({"files": []})

    results = []
    sources = VIDEO_DIRS if VIDEO_DIRS else _get_default_browse_dirs()
    for root_dir in sources:
        root = Path(root_dir)
        if not root.exists():
            continue
        for item in root.rglob("*"):
            if item.name.startswith("."):
                continue
            if item.suffix.lower() not in MEDIA_EXTS:
                continue
            if q in item.name.lower():
                results.append({
                    "path": str(item),
                    "name": item.name,
                    "type": item.suffix.lower(),
                })
            if len(results) >= 20:
                break
        if len(results) >= 20:
            break

    return jsonify({"files": results})

# ==================== 临时文件上传 ====================

@app.route("/api/upload-temp", methods=["POST"])
def api_upload_temp():
    """接收浏览器上传的文件，存到临时目录，返回本地路径"""
    if "file" not in request.files:
        return jsonify({"error": "无文件"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "空文件名"}), 400
    # 保持原文件名，存入 temp 目录
    dest = TEMP_DIR / f.filename
    f.save(str(dest))
    return jsonify({"path": str(dest), "name": f.filename})

# ==================== 上传 API ====================

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

    if not account_name:
        return jsonify({"error": "请选择账号"}), 400
    if not video_path:
        return jsonify({"error": "请选择视频"}), 400
    if not Path(video_path).exists():
        return jsonify({"error": f"视频文件不存在: {video_path}"}), 400

    profile_dir = account_mgr.get_profile_dir(account_name)
    if not profile_dir:
        return jsonify({"error": f"账号 {account_name} 不存在"}), 400

    global _upload_state
    _upload_state = {
        "running": True,
        "status": "开始上传...",
        "logs": [],
        "result": None,
    }

    def do_upload():
        async def _upload():
            global _upload_state
            uploader = WeChatUploader(profile_dir, headless=not _debug_mode)
            await uploader.start()

            if not await uploader.ensure_login():
                _upload_state["running"] = False
                _upload_state["status"] = "登录失败"
                _upload_state["logs"].append("登录失败，请先扫码登录")
                await uploader.close()
                return

            _upload_state["status"] = "上传中..."
            _upload_state["logs"].append(f"[开始] {title}")

            result = await uploader.upload_single(
                video_path=video_path,
                title=title,
                description=description,
                cover_path=cover_path,
                short_drama_name=short_drama_name,
                publish_time=publish_time,
                location=location,
            )
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

            await uploader.close()

            _cleanup_temp_files(video_path, cover_path)

            # 保存结果
            result_path = RESULTS_DIR / f"{account_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            with open(result_path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.DictWriter(f, fieldnames=["video_path", "title", "status", "error"])
                w.writeheader()
                w.writerow(result)

        asyncio.run(_upload())

    threading.Thread(target=do_upload, daemon=True).start()
    return jsonify({"message": "上传任务已启动"})

@app.route("/api/upload/status", methods=["GET"])
def api_upload_status():
    return jsonify(_upload_state)

def _cleanup_temp_files(*paths: str):
    """删除 TEMP_DIR 内的暂存文件"""
    for p in paths:
        if not p:
            continue
        fp = Path(p)
        if fp.parent == TEMP_DIR and fp.exists():
            fp.unlink(missing_ok=True)

# ==================== 启动 ====================

def main():
    import argparse
    parser = argparse.ArgumentParser(description="视频号批量上传 Web 面板")
    parser.add_argument("--port", type=int, default=5050, help="端口 (默认 5050)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--dirs", nargs="*", help="额外视频目录（空格分隔）")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    if args.dirs:
        VIDEO_DIRS.extend(args.dirs)

    print(f"视频号上传面板: http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=args.debug)

if __name__ == "__main__":
    main()
