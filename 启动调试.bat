@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

echo ============================================================
echo   TuneFree 源码调试启动器
echo   目录: %CD%
echo ============================================================
echo.

rem ---------- 1. 检查 Python ----------
where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 没找到 python，请先安装 Python 3 并勾选 Add to PATH
    pause
    exit /b 1
)
for /f "delims=" %%v in ('python -c "import sys;print(sys.version.split()[0])"') do set PYVER=%%v
echo [1/4] Python %PYVER%  OK

rem ---------- 2. 检查依赖 ----------
python -c "import PySide6" >nul 2>nul
if errorlevel 1 (
    echo [2/4] 缺少 PySide6，正在安装...
    python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple PySide6
) else (
    echo [2/4] PySide6  OK
)

python -c "import frida" >nul 2>nul
if errorlevel 1 (
    echo       缺少 frida，正在安装...
    python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple frida
) else (
    echo       frida     OK
)

rem ---------- 3. 检查源文件 ----------
if exist "main.py"            (echo [3/4] main.py  OK)           else (echo [错误] 缺少 main.py & pause & exit /b 1)
if exist "TuneFree.py"        (echo       TuneFree.py  OK)       else (echo [错误] 缺少 TuneFree.py & pause & exit /b 1)
if exist "version.py"         (echo       version.py  OK)        else (echo [警告] 缺少 version.py，版本号将退回内置值)
if exist "hook_qq_music.js"   (echo       hook_qq_music.js  OK)  else (echo [错误] 缺少 hook_qq_music.js & pause & exit /b 1)
if exist "ffmpeg.exe"         (echo       ffmpeg.exe  OK)        else (echo [错误] 缺少 ffmpeg.exe，转码会失败)
if exist "main.exe"           (echo       main.exe 存在，将优先使用打包版后端) else (echo       main.exe 不存在，将用 python main.py)

rem ---------- 4. 启动 ----------
if /i "%~1"=="--check" (
    echo [4/4] 检查模式，不启动界面
    exit /b 0
)

echo [4/4] 启动界面 ...
echo.
echo   提示：转码前先打开 QQ音乐并登录，把 .mflac / .mgg 歌放进一个文件夹再选它
echo.
python TuneFree.py
echo.
echo ============================================================
echo   程序已退出，返回码 %errorlevel%
echo ============================================================
pause
