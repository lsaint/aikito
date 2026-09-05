@echo off
setlocal
where python >nul 2>&1
if %ERRORLEVEL% equ 0 (
    python -X utf8 "%~dp0aikito" %*
    goto :end
)
where py >nul 2>&1
if %ERRORLEVEL% equ 0 (
    py -3 -X utf8 "%~dp0aikito" %*
    goto :end
)
where python3 >nul 2>&1
if %ERRORLEVEL% equ 0 (
    python3 -X utf8 "%~dp0aikito" %*
    goto :end
)
echo [ERROR] Python interpreter not found in PATH. >&2
exit /b 1

:end
exit /b %ERRORLEVEL%

