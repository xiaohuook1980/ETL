@echo off
chcp 65001 >nul
echo ========================================
echo   出款申请审核系统 启动中...
echo ========================================
cd /d "%~dp0"
python -m waitress --host=0.0.0.0 --port=5000 app:app
pause
