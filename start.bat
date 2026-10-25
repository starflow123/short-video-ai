@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0deploy"

echo ============================================
echo  myaivideo 一键启动
echo ============================================
echo.

REM --- 检查 docker ---
docker info >nul 2>&1
if errorlevel 1 (
  echo [错误] Docker 未运行。请先启动 Docker Desktop，等右下角鲸鱼图标变绿后再双击本脚本。
  echo.
  pause
  exit /b 1
)

echo [1/4] 拉取并构建镜像（首次约 3-5 分钟，之后走缓存）...
docker compose up -d --build
if errorlevel 1 (
  echo.
  echo [错误] docker compose 启动失败，完整日志见下方：
  docker compose logs --tail=50
  echo.
  pause
  exit /b 1
)

echo.
echo [2/4] 等待 postgres / redis 健康...
:wait_db
set /a tries=0
:wait_db_loop
timeout /t 2 /nobreak >nul
set /a tries+=1
docker compose ps postgres --format "{{.Status}}" | findstr "healthy" >nul 2>&1
if errorlevel 1 (
  if !tries! lss 30 goto wait_db_loop
  echo [警告] postgres 30 秒未 healthy，继续但可能异常
)

echo.
echo [3/4] 等待 backend 就绪（最多 90 秒）...
set /a tries=0
:wait_be
timeout /t 3 /nobreak >nul
set /a tries+=1
powershell -Command "try { (Invoke-WebRequest http://localhost:8000/docs -UseBasicParsing -TimeoutSec 2).StatusCode } catch { exit 1 }" >nul 2>&1
if errorlevel 1 (
  if !tries! lss 30 (
    echo   等待 backend... !tries!/30
    goto wait_be
  )
  echo [警告] backend 未就绪，查看日志：docker compose logs backend
)

echo.
echo [4/4] 当前容器状态：
docker compose ps
echo.
echo ============================================
echo  完成！
echo  前端:    http://localhost:7788
echo  后端文档: http://localhost:8000/docs
echo ============================================
echo.

start "" http://localhost:7788

endlocal
pause
