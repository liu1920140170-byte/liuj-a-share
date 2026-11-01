@echo off
rem Ask for a stock code, then refresh every 60 seconds
set /p code=Enter stock code, e.g. 600519 / sz300750 / sh000001: 
if "%code%"=="" set code=600519
call "%~dp0run.bat" %code% --loop 60
