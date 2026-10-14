@echo off
chcp 65001 >nul
cd /d "%~dp0"
title NAI 复现工具 - GUI

set "PY=python"
where python >nul 2>nul
if errorlevel 1 set "PY=C:\Users\Administrator\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"

echo ============================================
echo   NAI 复现工具 GUI
echo   浏览器访问 http://127.0.0.1:8770/
echo   关闭这个窗口即停止服务
echo ============================================
echo.

"%PY%" gui.py --port 8770 --open

echo.
echo [服务已退出] 上面若有报错信息请截图。
pause
