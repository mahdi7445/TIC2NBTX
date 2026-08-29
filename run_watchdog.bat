@echo off
REM این فایل executor.py را اجرا می‌کند و اگر به هر دلیلی (کرش، خطا،
REM قطعی اینترنت طولانی) متوقف شد، بعد از چند ثانیه دوباره اجرایش می‌کند.
REM این فایل باید همیشه در پس‌زمینه اجرا بماند - آن را نبندید.

cd /d "%~dp0"

:loop
echo [%date% %time%] در حال اجرای executor.py ...
python executor.py

echo [%date% %time%] executor.py متوقف شد. ۱۰ ثانیه دیگر دوباره اجرا می‌شود...
timeout /t 10 /nobreak > NUL
goto loop
