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

# 去掉用不到的 Qt 组件（2026-11-09 精简，实测省下约 21MB）：
# - 整个模块既没被 import、也没被任何 dll 的导入表引用（用 PE 导入表扫过一遍）
# - 插件里只保留 qwindows 平台、qico 图标、现代 Windows 样式
# 如果以后换了 Qt 版本导致启动报缺 dll，把对应名字从这里去掉即可。
_DROP = (
    'opengl32sw',            # 软件 OpenGL 兜底，20MB 级的包袱
    'qt6quick', 'qt6qml', 'qt6pdf', 'qt6designer',
    'qt6opengl', 'qt6network', 'qtnetwork.pyd', 'qt6svg', 'qt6virtualkeyboard',
    'qdirect2d',             # Direct2D 平台插件，只有 QT_QPA_PLATFORM=direct2d 才用
    'qtuiotouchplugin', 'qtvirtualkeyboardplugin', 'qnetworklistmanager',
    'qopensslbackend', 'qschannelbackend', 'qcertonlybackend', 'qsvgicon',
    'imageformats\\qjpeg', 'imageformats\\qwebp', 'imageformats\\qtiff',
    'imageformats\\qtga', 'imageformats\\qwbmp', 'imageformats\\qicns',
    'imageformats\\qpdf', 'imageformats\\qgif', 'imageformats\\qsvg',
    'libcrypto-3-x64', 'libssl-3-x64',   # OpenSSL 的重复副本，_ssl.pyd 只用不带后缀的
)
a.binaries = [b for b in a.binaries if not any(k in b[0].lower() for k in _DROP)]

# Qt 翻译只留简体中文，其余 90 多个 .qm 白占 6MB
a.datas = [d for d in a.datas
           if 'pyside6\\translations' not in d[0].lower() or 'zh_cn' in d[0].lower()]

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
