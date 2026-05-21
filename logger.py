"""
统一日志模块
FileHandler 始终写入文件，StreamHandler 仅在 stdout 可用时启用（兼容 PyInstaller windowed 模式）
"""
import logging
import sys
from pathlib import Path
from datetime import datetime


def _get_log_dir() -> Path:
    """日志目录：data/logs/"""
    base = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
    d = base / "data" / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _get_log_file() -> Path:
    """按天轮转的日志文件"""
    return _get_log_dir() / f"app_{datetime.now().strftime('%Y-%m-%d')}.log"


_FORMATTER = logging.Formatter(
    "%(asctime)s | %(levelname)-5s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

_ROOT = logging.getLogger("wechat")
_ROOT.setLevel(logging.DEBUG)

# FileHandler — 始终启用
_fh = logging.FileHandler(str(_get_log_file()), encoding="utf-8")
_fh.setLevel(logging.DEBUG)
_fh.setFormatter(_FORMATTER)
_ROOT.addHandler(_fh)

# StreamHandler — 仅在 stdout 可用时启用（windowed 模式下 sys.stdout 为 None）
if sys.stdout:
    _sh = logging.StreamHandler(sys.stdout)
    _sh.setLevel(logging.DEBUG)
    _sh.setFormatter(_FORMATTER)
    _ROOT.addHandler(_sh)


def get_logger(name: str) -> logging.Logger:
    """获取子 logger，统一使用 wechat 命名空间"""
    return _ROOT.getChild(name)


def set_level(level: int):
    """动态调整日志级别"""
    _ROOT.setLevel(level)
