@echo off
setlocal
where python >nul 2>&1
if %ERRORLEVEL% equ 0 (
    python "%~dp0aikito" %*
    goto :end
)
where py >nul 2>&1
if %ERRORLEVEL% equ 0 (
    py -3 "%~dp0aikito" %*
    goto :end
)
where python3 >nul 2>&1
if %ERRORLEVEL% equ 0 (
    python3 "%~dp0aikito" %*
    goto :end
)
echo [ERROR] Python interpreter not found in PATH. >&2
exit /b 1

:end
exit /b %ERRORLEVEL%

