# -*- mode: python ; coding: utf-8 -*-


import os as _os
_msroot = _os.path.expandvars(r'%LOCALAPPDATA%\ms-playwright')
_ms_browsers = [
    (_msroot + '/chromium_headless_shell-1208', 'ms-playwright/chromium_headless_shell-1208'),
    (_msroot + '/ffmpeg-1011', 'ms-playwright/ffmpeg-1011'),
    (_msroot + '/winldd-1007', 'ms-playwright/winldd-1007'),
]

a = Analysis(
    [_os.path.join(SPECPATH, 'app.py')],
    pathex=[SPECPATH],
    binaries=[],
    datas=[
        (_os.path.join(SPECPATH, 'templates'), 'templates'),
    ] + _ms_browsers,
    hiddenimports=['accounts', 'uploader', 'logger', 'csv_generator'],
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
    upx_exclude=[],
    name='视频号上传',
)
