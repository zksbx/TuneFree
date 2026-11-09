# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：一个入口 app.py，产出只有一个 TuneFree.exe。

用法（在项目根目录）：
    pyinstaller --noconfirm --clean TuneFree.spec

产物：dist/TuneFree/  ->  TuneFree.exe + _internal/（其余都在 _internal 里）
"""

from PyInstaller.utils.hooks import collect_all

datas = [
    ('hook_qq_music.js', '.'),
    ('ffmpeg.exe', '.'),
    ('赞赏码.png', '.'),
    ('使用说明.html', '.'),
    ('tunefree.ico', '.'),
]
binaries = []
hiddenimports = []

# frida 带原生 .pyd / .dll，交给 hook 全量收集
for pkg in ('frida',):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'unittest', 'pydoc', 'doctest', 'test', 'pdb'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

# 去掉用不到的 Qt 组件：opengl32sw 是软件 OpenGL 兜底（20MB），Quick/Qml/Pdf 这套界面用不上。
# 实测删掉后界面和转码都正常；如果以后换了 Qt 版本导致启动报缺 dll，把对应名字从这里去掉即可。
_DROP = ('opengl32sw', 'qt6quick', 'qt6qml', 'qt6pdf', 'qt6designer')
a.binaries = [b for b in a.binaries if not any(k in b[0].lower() for k in _DROP)]

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='TuneFree',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,           # 窗口程序：双击不弹黑框；子进程靠管道拿日志
    disable_windowed_traceback=False,
    icon='tunefree.ico',
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='TuneFree',
)
