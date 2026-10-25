@echo off
echo Installing libraries...
pip install -r requirements.txt pyinstaller
echo Building the app...
pyinstaller --onefile --noconsole --name PrototypeAgent --add-data "templates;templates" --add-data "static;static" app.py
echo.
echo Done! Your app is: dist\PrototypeAgent.exe
pause
