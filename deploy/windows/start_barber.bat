@echo off
chcp 65001 >nul
title نظام إدارة الحلاقة

REM ينتقل تلقائياً لمجلد المشروع (مهما كان مسار التثبيت)
cd /d "%~dp0..\.."

if not exist "venv\Scripts\python.exe" (
    echo.
    echo [خطأ] لم يُعثر على Python الافتراضي: venv\Scripts\python.exe
    echo تأكد من تثبيت المشروع كاملاً مع مجلد venv.
    echo.
    pause
    exit /b 1
)

echo.
echo ========================================
echo   نظام إدارة الحلاقة — يعمل الآن
echo ========================================
echo.
echo   لا تغلق هذه النافذة أثناء العمل.
echo   لإيقاف النظام: أغلق هذه النافذة أو شغّل stop_barber.bat
echo.
echo   المتصفح: http://127.0.0.1:8000/
echo ========================================
echo.

start "" "http://127.0.0.1:8000/"

"venv\Scripts\python.exe" manage.py runserver 127.0.0.1:8000 --noreload

echo.
echo تم إيقاف الخادم.
pause
