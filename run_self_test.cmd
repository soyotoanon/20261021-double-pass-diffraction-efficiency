@echo off
setlocal
cd /d "%~dp0"
python -m unittest -v test_core.py test_scan_integration.py test_zero_scan.py
pause
endlocal
