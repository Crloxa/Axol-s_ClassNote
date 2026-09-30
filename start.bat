@echo off
rem 课堂复习 Agent - 本机启动脚本（只监听 127.0.0.1:18471）
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [错误] 未找到 Python 虚拟环境。请先执行：
  echo   python -m venv .venv
  echo   .venv\Scripts\pip install -r backend\requirements.txt
  pause & exit /b 1
)

if not exist "frontend\dist\index.html" (
  echo [错误] 前端尚未构建。请先执行：
  echo   cd frontend ^&^& npm install ^&^& npm run build
  pause & exit /b 1
)

echo 启动课堂复习 Agent： http://127.0.0.1:18471  （Ctrl+C 退出）
cd backend
"..\.venv\Scripts\python.exe" run.py
