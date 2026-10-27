@echo off
cd /d "%~dp0"
set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=python"

echo Toca da Capivara: a parar qualquer servidor antigo na porta 8765...
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }"

echo Toca da Capivara: a atualizar o yt-dlp (o YouTube muda muitas vezes)...
"%PY%" -m pip install -U "yt-dlp[default,curl-cffi]" -q --disable-pip-version-check
"%PY%" -m pip install -U --pre --no-deps yt-dlp -q --disable-pip-version-check

echo Toca da Capivara: a iniciar em http://127.0.0.1:8765
echo Pasta em uso: %~dp0
echo Fecha esta janela para parar o servidor.
start "" cmd /c "timeout /t 3 /nobreak >nul & start http://127.0.0.1:8765"
"%PY%" -m uvicorn main:app --app-dir "%~dp0" --port 8765
pause
