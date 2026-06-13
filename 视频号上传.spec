# -*- mode: python ; coding: utf-8 -*-


import os as _os
import sys as _sys
import shutil as _shutil
from pathlib import Path as _Path

# === CloakBrowser stealth Chromium — 仅删语言包 ===
_cb_datas = []
try:
    from cloakbrowser.download import binary_info as _cb_info
    _cb_src = _Path(_cb_info()['cache_dir'])
    if _cb_src.exists():
        _staging = _Path(SPECPATH) / '_cloak_staging'
        if _staging.exists():
            _shutil.rmtree(_staging)
        _staging.mkdir(parents=True)
        for _f in _cb_src.iterdir():
            if _f.is_dir() and _f.name == 'locales':
                _loc_dst = _staging / 'locales'
                _loc_dst.mkdir()
                _zh = _f / 'zh-CN.pak'
                if _zh.exists():
                    _shutil.copy2(_zh, _loc_dst / 'zh-CN.pak')
                continue
            elif _f.is_dir():
                _shutil.copytree(_f, _staging / _f.name)
            else:
                _shutil.copy2(_f, _staging / _f.name)
        _cb_datas = [(_os.fspath(_staging), 'cloakbrowser')]
except Exception:
    pass

# === Playwright driver（CloakBrowser 底层依赖）===
_pw_driver = []
try:
    import playwright as _pw
    _pw_dir = _Path(_pw.__file__).parent / 'driver'
    if _pw_dir.exists():
        _pw_driver = [(_os.fspath(_pw_dir), 'playwright/driver')]
except Exception:
    pass

a = Analysis(
    [_os.path.join(SPECPATH, 'app.py')],
    pathex=[SPECPATH],
    binaries=[],
    datas=[
        (_os.path.join(SPECPATH, 'templates'), 'templates'),
        (_os.path.join(SPECPATH, 'icon.ico'), '.'),
        (_os.path.join(SPECPATH, 'icon.svg'), '.'),
    ] + _cb_datas + _pw_driver,
    hiddenimports=['accounts', 'uploader', 'logger', 'plyer', 'pystray', 'PIL', 'cloakbrowser'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='视频号上传',
    icon='icon.ico',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=['cloakbrowser'],
    name='视频号上传',
)
