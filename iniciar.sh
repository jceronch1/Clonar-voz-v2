#!/usr/bin/env bash
# Arranque en Linux y macOS. En Windows usa iniciar.bat
set -euo pipefail
cd "$(dirname "$0")"

echo
echo "  ============================================"
echo "   Clonador de voz local  -  Qwen3-TTS 1.7B"
echo "  ============================================"
echo

PY=$(command -v python3 || command -v python || true)
if [ -z "$PY" ]; then
  echo "  [X] No se encuentra Python 3. Instálalo y vuelve a intentarlo."
  exit 1
fi

# ---------- Dependencias ----------
if ! "$PY" -c "import fastapi, uvicorn, multipart" >/dev/null 2>&1; then
  echo "  [*] Instalando dependencias de Python..."
  "$PY" -m pip install --quiet --disable-pip-version-check -r requirements.txt
  echo "  [OK] Dependencias instaladas."
fi

# ---------- llama.cpp ----------
if ! command -v llama-tts >/dev/null 2>&1; then
  echo
  echo "  [!] No se encuentra llama-tts. Hace falta llama.cpp b10500 o superior."
  echo "      macOS:  brew install llama.cpp"
  echo "      Linux:  compílalo desde https://github.com/ggml-org/llama.cpp"
  echo "              (o descarga un binario de sus releases)"
  echo
fi

# ---------- Modelos ----------
if ! "$PY" -c "import descargar_modelo,sys; sys.exit(1 if descargar_modelo.falta_algo() else 0)" >/dev/null 2>&1; then
  echo "  [!] Faltan los modelos .gguf (unos 1,5 GB). Se descargan una sola vez."
  read -r -p "      ¿Descargarlos ahora? [s/N] " RESP
  if [[ "$RESP" =~ ^[SsYy]$ ]]; then
    "$PY" descargar_modelo.py
  else
    echo "  [i] Podrás descargarlos desde la propia web al abrirla."
  fi
fi

# ---------- Arrancar ----------
echo
echo "  [*] Arrancando el servidor..."
( sleep 2; (command -v xdg-open >/dev/null && xdg-open http://127.0.0.1:8080) \
  || (command -v open >/dev/null && open http://127.0.0.1:8080) || true ) &
exec "$PY" app.py
