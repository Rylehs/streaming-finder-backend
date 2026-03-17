@echo off
echo Démarrage du backend Streaming Finder...
cd /d "%~dp0"
C:\Users\SergeCopily\AppData\Local\Programs\Python\Python312\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
