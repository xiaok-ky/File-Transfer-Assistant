# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包：onedir + 无控制台窗口（GUI 应用）。"""

a = Analysis(
    ['wifi_gui.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['qrcode', 'qrcode.main', 'qrcode.util', 'qrcode.base',
                   'qrcode.constants', 'qrcode.image.pure', 'wifi_core'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'unittest', 'doctest', 'pytest', 'pip', 'setuptools',
        'numpy', 'pandas', 'matplotlib', 'scipy', 'PIL',
        'pydoc_data',
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='传输助手',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # 关键：不要黑色控制台窗口
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='传输助手',
)
