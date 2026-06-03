@echo off
chcp 65001 >nul
title إيقاف نظام الحلاقة

echo.
echo جاري إيقاف خادم النظام على المنفذ 8000...
echo.

for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8000" ^| findstr "LISTENING"') do (
    taskkill /PID %%a /F >nul 2>&1
    if not errorlevel 1 echo تم إيقاف العملية %%a
)

echo.
echo إن كان النظام لا يزال يعمل، أغلق نافذة "نظام إدارة الحلاقة" السوداء يدوياً.
echo.
pause
