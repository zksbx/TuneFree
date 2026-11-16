@echo off
chcp 936 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set "PY=C:\Program Files (x86)\python3.12.10\python.exe"
if not exist "%PY%" set "PY=python"

echo ============================================================
echo   TuneFree 源码调试启动器（v3 新界面）
echo   目录: %CD%
echo ============================================================
echo.

"%PY%" -c "import PySide6, frida" >nul 2>nul
if errorlevel 1 goto nodep

echo [1/2] Python: %PY%
echo [2/2] 启动界面 ...（关掉这个黑窗口 = 退出程序）
echo.
echo   提醒：先打开 QQ音乐并登录，再回这里选文件夹；
echo         下面这些文字是程序日志，报错时把最后几十行发我。
echo.
"%PY%" app.py
set RC=%errorlevel%
echo.
echo ============================================================
echo   程序已退出，返回码 %RC%
echo ============================================================
pause
exit /b 0

:nodep
echo [错误] 没找到同时装了 PySide6 和 frida 的 Python。
echo        请先执行：
echo          python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple PySide6 frida
pause
exit /b 1
