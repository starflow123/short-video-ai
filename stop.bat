@echo off
chcp 65001 >nul
setlocal

cd /d "%~dp0deploy"

echo ============================================
echo  myaivideo 停止服务
echo ============================================
echo.

docker compose down

echo.
echo 已停止。数据（postgres / redis / media）保留在 docker volume 里，下次 start.bat 会接着用。
echo 如需彻底清空数据，运行: docker compose down -v
echo.

endlocal
pause
