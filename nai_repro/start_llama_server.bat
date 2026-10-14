@echo off
chcp 65001 >nul
cd /d "%~dp0"
title llama.cpp - Huihui Spark X2.5 4B

rem ============================================================
rem  启动本地 llama.cpp 服务（Spark 4B abliterated）
rem  Ollama 不支持 spark2_5 架构，所以这个模型只能走 llama.cpp
rem  服务地址 http://127.0.0.1:8081
rem ============================================================

set "BIN=E:\llama_bin\cuda"
set "MODEL=E:\llama_models\Huihui-Spark-X2.5-4B-abliterated-Q4_K_M.gguf"

if not exist "%BIN%\llama-server.exe" (
  echo [错误] 找不到 %BIN%\llama-server.exe
  echo   需要 llama.cpp 的 Windows CUDA 版：llama-b11582-bin-win-cuda-12.4-x64.zip
  pause & exit /b 1
)
if not exist "%MODEL%" (
  echo [错误] 找不到模型文件 %MODEL%
  pause & exit /b 1
)

cd /d "%BIN%"
echo ============================================
echo   llama.cpp server  http://127.0.0.1:8081
echo   模型: Huihui-Spark-X2.5-4B-abliterated Q4_K_M
echo   关闭本窗口即停止服务
echo ============================================
echo.

llama-server.exe -m "%MODEL%" ^
  -c 16384 -np 1 -ngl 99 -fa on ^
  --host 127.0.0.1 --port 8081 ^
  -a huihui-spark-4b

echo.
echo [服务已退出]
pause
