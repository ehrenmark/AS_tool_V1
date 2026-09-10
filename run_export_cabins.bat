@echo off
setlocal
"%~dp0.venv\Scripts\python.exe" "%~dp0jobs\export_cabins.py" %*
endlocal
