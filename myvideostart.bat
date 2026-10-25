@echo off
chcp 65001 >nul
setlocal

cd /d "%~dp0deploy"

echo ============================================
echo  myaivideo 一键启动
echo ============================================
echo.

echo [1/3] 启动 postgres / redis / backend / worker / frontend ...
docker compose up -d --build
if errorlevel 1 (
  echo.
  echo [错误] docker compose 启动失败，请检查 Docker Desktop 是否在运行
  pause
  exit /b 1
)

echo.
echo [2/3] 等待 backend 就绪 ...
set /a tries=0
:wait_loop
timeout /t 3 /nobreak >nul
set /a tries+=1
docker compose exec -T backend curl -fs http://localhost:8000/api/health >nul 2>&1
if errorlevel 1 (
  if %tries% lss 30 (
    echo   等待中... (%tries%/30)
    goto wait_loop
  )
  echo [警告] backend 30 次等待后仍未就绪，请查看 docker compose logs backend
)

echo.
echo [3/3] 服务状态：
docker compose ps

echo.
echo ============================================
echo  全部启动完成
echo  前端:    http://localhost:7788
echo  后端:    http://localhost:8000/docs
echo ============================================
echo.

start http://localhost:7788

endlocal
pause
