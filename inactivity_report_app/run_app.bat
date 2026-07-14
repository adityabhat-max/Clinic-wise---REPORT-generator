@echo off
cd /d "%~dp0"
"C:\Users\User\AppData\Local\Programs\Python\Python312\python.exe" -m streamlit run app.py
echo.
echo Streamlit stopped or failed to start. See any error above.
pause
