@echo off
setlocal
"%~dp0.venv\Scripts\python.exe" "%~dp0jobs\import_cabins.py" %*
endlocal
