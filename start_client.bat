@echo off
rem CodeClash player app for Windows.
rem Double-click this file, or run  .\start_client.bat  from PowerShell or cmd in the project folder.
rem First run: creates .\venv and installs Streamlit (needs internet once). Later runs work offline.
cd /d "%~dp0"

where py >nul 2>nul && (set "PY=py -3") || (set "PY=python")

if not exist venv\Scripts\python.exe (
  echo Creating the virtual environment - first run only...
  %PY% -m venv venv || goto :nopython
)

venv\Scripts\python.exe -m pip install --disable-pip-version-check -q -r requirements.txt || goto :pipfail

echo.
echo Starting CodeClash. Your browser opens http://localhost:8501
echo Type the MacBook's IP address on the first screen. Close this window to quit.
echo.
rem Open the browser after 4 seconds, while Streamlit starts in this window.
start "" /min cmd /c "timeout /t 4 /nobreak >nul & start "" http://localhost:8501"
venv\Scripts\python.exe -m streamlit run app.py --server.address 127.0.0.1 --server.port 8501
pause
exit /b 0

:nopython
echo.
echo Python was not found. Install Python 3.11+ from https://www.python.org/downloads/
echo and tick "Add python.exe to PATH" during the installation, then run this file again.
pause
exit /b 1

:pipfail
echo.
echo Installing Streamlit failed. Check the internet connection, then run this file again.
pause
exit /b 1
