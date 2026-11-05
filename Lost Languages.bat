@echo off
REM Lost Languages - start the app
cd /d "%~dp0"
if not exist ".installed" (
  echo Installing Python packages, first run only...
  python -m pip install --user -q -r requirements.txt && echo ok> .installed
)
start "" pythonw "app\lost_languages_app.py"
