"""
多账号管理模块
每个账号独立 Chrome profile 目录
"""
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Optional
import sys

_BASE_DIR = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent

DATA_DIR = _BASE_DIR / "data"
ACCOUNTS_FILE = DATA_DIR / "accounts.json"
ACCOUNTS_ROOT = _BASE_DIR / "accounts"

_NAME_RE = re.compile(r'^[a-zA-Z0-9_\-\u4e00-\u9fff]{1,50}$')


def load_accounts() -> dict:
    """加载账号列表"""
    if not ACCOUNTS_FILE.exists():
        return {"accounts": {}}

    try:
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"accounts": {}}


def save_accounts(data: dict):
    """保存账号列表（原子写入，防止崩溃时数据丢失）"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(DATA_DIR), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, str(ACCOUNTS_FILE))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def validate_account_name(name: str) -> bool:
    """校验账号名称是否合法"""
    return bool(_NAME_RE.match(name))


def add_account(name: str, nickname: str = "") -> dict:
    """
    添加新账号
    返回更新后的账号信息
    """
    if not validate_account_name(name):
        return {"error": f"账号名称不合法，仅支持中英文、数字、下划线、连字符（1-50字符）"}

    data = load_accounts()
    if name in data["accounts"]:
        return {"error": f"账号 {name} 已存在"}

    profile_dir = ACCOUNTS_ROOT / name / "profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    account = {
        "name": name,
        "nickname": nickname or name,
        "profile_dir": str(profile_dir),
        "created_at": _timestamp(),
        "last_login": "",
    }
    data["accounts"][name] = account
    save_accounts(data)
    return {"success": True, "account": data["accounts"][name]}


def remove_account(name: str) -> dict:
    """删除账号（不删除 profile 目录，安全起见）"""
    data = load_accounts()
    if name not in data["accounts"]:
        return {"error": f"账号 {name} 不存在"}

    del data["accounts"][name]
    save_accounts(data)
    return {"success": True}


def get_account(name: str) -> Optional[dict]:
    """获取单个账号信息"""
    data = load_accounts()
    return data["accounts"].get(name)


def list_accounts() -> list:
    """列出所有账号"""
    data = load_accounts()
    return list(data["accounts"].values())


def get_profile_dir(name: str) -> Optional[Path]:
    """获取账号 profile 目录"""
    account = get_account(name)
    if account:
        return Path(account["profile_dir"])
    return None


def update_last_login(name: str):
    """更新最后登录时间"""
    data = load_accounts()
    if name in data["accounts"]:
        data["accounts"][name]["last_login"] = _timestamp()
        save_accounts(data)


def clear_last_login(name: str):
    """清除最后登录时间（标记账号失效）"""
    data = load_accounts()
    if name in data["accounts"]:
        data["accounts"][name]["last_login"] = ""
        save_accounts(data)


def _timestamp() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("账号管理工具")
        print("  python accounts.py list          - 列出所有账号")
        print("  python accounts.py add <名称>    - 添加账号")
        print("  python accounts.py remove <名称> - 删除账号")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "list":
        accounts = list_accounts()
        if not accounts:
            print("暂无账号，请先用 add 命令添加")
        for a in accounts:
            print(f"  {a['name']:20s} | {a['nickname']:15s} | 最后登录: {a.get('last_login', '')}")

    elif cmd == "add" and len(sys.argv) > 2:
        name = sys.argv[2]
        nickname = sys.argv[3] if len(sys.argv) > 3 else ""
        result = add_account(name, nickname)
        print(result)

    elif cmd == "remove" and len(sys.argv) > 2:
        name = sys.argv[2]
        result = remove_account(name)
        print(result)
