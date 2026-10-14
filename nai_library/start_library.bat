@echo off
cd /d "%~dp0"
title NAI 资源库
set PYTHONIOENCODING=utf-8

rem ============================================================
rem  挑一个装了 Pillow 的 Python 再启动。
rem  PATH 里第一个 python 未必装了依赖，直接用会报
rem  ModuleNotFoundError: No module named 'PIL' 然后闪退。
rem
rem  !! 本文件必须以 GBK 存储，不能存成 UTF-8 !!
rem  cmd.exe 按系统代码页读批处理，UTF-8 的中文会被拆坏整行。
rem ============================================================

set "BUNDLED=C:\Users\Administrator\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"

set "PY="

rem ---- 1) PATH 里的 python，前提是能 import PIL ----
where python >nul 2>nul
if errorlevel 1 goto try_bundled
python -c "import PIL" >nul 2>nul
if errorlevel 1 goto try_bundled
set "PY=python"
goto run

rem ---- 2) 退回内置运行时 ----
:try_bundled
if not exist "%BUNDLED%" goto no_python
"%BUNDLED%" -c "import PIL" >nul 2>nul
if errorlevel 1 goto no_python
set "PY=%BUNDLED%"
goto run

rem ---- 3) 都不行，明确说清楚怎么修 ----
:no_python
echo.
echo  [错误] 找不到装了 Pillow 的 Python，无法启动。
echo.
echo  当前 PATH 里的 python：
where python 2>nul
echo.
echo  解决办法（任选其一）：
echo    1. 给现有 Python 装依赖：  python -m pip install Pillow
echo    2. 或把可用的 python.exe 完整路径填进本文件的 BUNDLED 变量
echo.
pause
exit /b 1

:run
echo ============================================
echo   NAI 资源库
echo   解释器: %PY%
echo   浏览器访问 http://127.0.0.1:8780/
echo   关闭这个窗口即停止服务
echo ============================================
echo.

"%PY%" lib_server.py --port 8780 --open

echo.
echo [服务已退出] 上面若有报错信息请截图。
pause
