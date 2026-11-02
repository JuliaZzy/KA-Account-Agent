@echo off
rem One entry point: every command runs inside Docker Compose. Nothing starts on the host.
rem   dev            desk (Vite) + API + Celery worker + Postgres + Redis + Milvus   http://localhost:5173
rem   dev api        API + Celery worker + Postgres + Redis + Milvus                 http://127.0.0.1:8000/docs
rem   dev kill       stop the stack (keeps the Postgres volume)
rem   dev db         start Postgres and wait until it is ready
rem   dev catalog    import the Open Food Facts snapshot into the product catalog
rem   dev test       pytest (Python). Frontend: dev test-web
rem   dev lint       ruff
rem   dev py ...     python inside the api container
rem   dev build      rebuild images
setlocal
cd /d "%~dp0"

where docker >nul 2>&1
if errorlevel 1 (
  echo Docker is not installed ^(or not on PATH^). Install Docker Desktop, then retry.
  exit /b 1
)
docker info >nul 2>&1
if errorlevel 1 (
  echo Docker is not running. Start Docker Desktop, then retry.
  exit /b 1
)

set "CMD=%~1"
if "%CMD%"=="" set "CMD=ui"
if /i "%CMD%"=="ui"   goto :ui
if /i "%CMD%"=="web"  goto :ui
if /i "%CMD%"=="up"   goto :ui
if /i "%CMD%"=="api"  goto :api
if /i "%CMD%"=="kill" goto :down
if /i "%CMD%"=="stop" goto :down
if /i "%CMD%"=="down" goto :down
if /i "%CMD%"=="build" ( docker compose build %2 %3 %4 & exit /b )
if /i "%CMD%"=="db"   goto :db
if /i "%CMD%"=="catalog" ( docker compose run --rm api python scripts/build_catalog.py %2 %3 %4 & exit /b )
if /i "%CMD%"=="test" ( docker compose run --rm api python -m pytest -q %2 %3 %4 & exit /b )
if /i "%CMD%"=="test-web" ( docker compose run --rm --no-deps frontend npm test %2 %3 %4 & exit /b )
if /i "%CMD%"=="lint" ( docker compose run --rm --no-deps api python -m ruff check backend tests scripts & exit /b )
if /i "%CMD%"=="py"   ( docker compose run --rm api python %2 %3 %4 %5 %6 %7 %8 %9 & exit /b )
echo Unknown command: %CMD%   (use: ui api kill db catalog test test-web lint py build)
exit /b 1

:needenv
if not exist "%~dp0.env" (
  echo No .env — copy .env.example to .env and fill in the model key.
  echo   copy .env.example .env
  exit /b 1
)
exit /b 0

:ui
call :needenv
if errorlevel 1 exit /b 1
docker compose up %2 %3 %4
exit /b

:api
call :needenv
if errorlevel 1 exit /b 1
docker compose up postgres redis milvus api worker %2 %3 %4
exit /b

:down
docker compose down %2 %3 %4
exit /b

:db
docker compose up -d postgres
docker compose exec postgres pg_isready -U ka_account -d ka_account
exit /b
