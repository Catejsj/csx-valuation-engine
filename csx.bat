@echo off
rem Windows: double-click this file, or type  csx  in this folder.
setlocal
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY goto nopython
%PY% "%~dp0csx.py" %*
set "RC=%errorlevel%"
if "%RC%"=="9009" goto nopython
if not "%RC%"=="0" if "%~1"=="" pause
exit /b %RC%
:nopython
echo.
echo  Python is not installed yet.
echo  1. Get it from https://www.python.org/downloads/
echo  2. While installing, tick "Add python.exe to PATH".
echo  3. Double-click csx.bat again.
echo.
pause
exit /b 1
