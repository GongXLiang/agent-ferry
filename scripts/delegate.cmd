@echo off
rem agent-delegate-verify bootstrap for cmd.exe: runs delegate.ps1, which finds Python 3.10+.
rem Arguments pass through cmd.exe, so put long or quoted task text in a UTF-8 file (--prompt-file).
setlocal
set "ADV_PS=powershell.exe"
where pwsh.exe >nul 2>nul && set "ADV_PS=pwsh.exe"
"%ADV_PS%" -NoLogo -NoProfile -ExecutionPolicy RemoteSigned -File "%~dp0delegate.ps1" %*
exit /b %ERRORLEVEL%
