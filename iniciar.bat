@echo off
chcp 65001 >nul
title Clonador de voz local - Qwen3-TTS
cd /d "%~dp0"

echo.
echo   ============================================
echo    Clonador de voz local  -  Qwen3-TTS 1.7B
echo   ============================================
echo.

REM ---------- 1. Python ----------
where python >nul 2>&1
if errorlevel 1 (
    echo  [X] No se encuentra Python. Instalalo desde https://www.python.org/downloads/
    echo      y marca la casilla "Add python.exe to PATH".
    pause
    exit /b 1
)

REM ---------- 2. Dependencias ----------
python -c "import fastapi, uvicorn, multipart" >nul 2>&1
if errorlevel 1 (
    echo  [*] Instalando dependencias de Python...
    python -m pip install --quiet --disable-pip-version-check -r requirements.txt
    if errorlevel 1 (
        echo  [X] Fallo la instalacion de dependencias.
        pause
        exit /b 1
    )
    echo  [OK] Dependencias instaladas.
)

REM ---------- 3. llama.cpp ----------
where llama-tts >nul 2>&1
if errorlevel 1 (
    echo.
    echo  [!] No se encuentra llama-tts. Se necesita llama.cpp b10500 o superior.
    echo.
    set /p RESP="     Instalarlo ahora con winget? [S/N] "
    if /i "%RESP%"=="S" (
        winget install --id ggml.llamacpp --accept-source-agreements --accept-package-agreements
        echo.
        echo  [!] Cierra esta ventana y vuelve a ejecutar iniciar.bat
        echo      para que Windows reconozca el nuevo PATH.
        pause
        exit /b 0
    )
)

REM ---------- 4. Modelos ----------
python -c "import descargar_modelo,sys; sys.exit(1 if descargar_modelo.falta_algo() else 0)" >nul 2>&1
if errorlevel 1 (
    echo.
    echo  [!] Faltan los modelos .gguf ^(unos 1,5 GB^). Se descargan una sola vez.
    echo.
    set /p RESP="     Descargarlos ahora? [S/N] "
    if /i "%RESP%"=="S" (
        python descargar_modelo.py
        if errorlevel 1 (
            echo  [X] La descarga no termino. Puedes reintentarla: los archivos se reanudan.
            pause
            exit /b 1
        )
    ) else (
        echo  [i] Podras descargarlos desde la propia web al abrirla.
    )
)

REM ---------- 5. Arrancar ----------
echo.
echo  [*] Arrancando el servidor...
start "" http://127.0.0.1:8080
python app.py

echo.
echo  El servidor se ha detenido.
pause
