# Carpeta del modelo

Aquí van los **dos** archivos `.gguf` que necesita la aplicación. No están en el
repositorio porque pesan alrededor de 1,5 GB.

| Archivo | Qué es |
|---|---|
| `Qwen3-TTS-12Hz-1.7B-Base-Q4_K_M.gguf` | el modelo de lenguaje que produce los tokens de audio |
| `mmproj-Qwen3-TTS-12Hz-1.7B-Base-Q8_0.gguf` | el proyector/vocoder que convierte esos tokens en sonido |

**Hacen falta los dos.** Sin el `mmproj`, `llama-tts` carga el modelo pero no
genera ningún audio.

## Cómo conseguirlos

Cualquiera de estas tres vías deja los archivos en esta misma carpeta:

1. `iniciar.bat` (Windows) o `./iniciar.sh` (Linux/macOS) los ofrece al arrancar.
2. `python descargar_modelo.py` desde la terminal.
3. El panel «Primer arranque» que aparece en la propia web.

La descarga es reanudable y verifica el SHA-256 de cada archivo al terminar.

## Descarga manual

Si prefieres bajarlos a mano, están en
<https://huggingface.co/ggml-org/Qwen3-TTS-12Hz-1.7B-Base-GGUF/tree/main>.
Déjalos en esta carpeta tal cual, sin renombrarlos.

Hay otras cuantizaciones disponibles (Q8_0 y BF16, con algo más de calidad y más
peso). La aplicación detecta automáticamente cualquier par modelo + `mmproj-*`
que encuentre aquí.
