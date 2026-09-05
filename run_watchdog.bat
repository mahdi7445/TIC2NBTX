@echo off
setlocal
cd /d "%~dp0"

:loop
echo [%date% %time%] Starting Nobitex executor...
python executor.py

echo [%date% %time%] Executor stopped. Restarting in 10 seconds...
timeout /t 10 /nobreak > NUL
goto loop
