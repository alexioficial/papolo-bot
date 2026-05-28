@echo off
setlocal

cd /d "%~dp0"

echo [sync] git pull...
git pull
if errorlevel 1 goto :error

echo.
echo [sync] git submodule update --init --recursive...
git submodule update --init --recursive
if errorlevel 1 goto :error

echo.
echo [sync] OK
exit /b 0

:error
echo.
echo [sync] FAIL
exit /b 1
