@echo off
chcp 65001 >nul
cd /d "%~dp0"
title NAI 资源库

set "PY=python"
where python >nul 2>nul
if errorlevel 1 set "PY=C:\Users\Administrator\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"

echo ============================================
echo   NAI 资源库
echo   浏览器访问 http://127.0.0.1:8780/
echo   关闭这个窗口即停止服务
echo ============================================
echo.

"%PY%" lib_server.py --port 8780 --open

echo.
echo [服务已退出] 上面若有报错信息请截图。
pause
