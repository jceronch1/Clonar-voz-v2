# -*- coding: utf-8 -*-
"""
Clonador de voz local — Qwen3-TTS (GGUF) + llama.cpp

Servidor web que expone una interfaz para clonar voces y sintetizar texto
usando el binario `llama-tts` de llama.cpp. Funciona igual en CPU y en GPU:
el modo se elige en cada petición mediante -ngl / --device / -mmdev.

Uso:  python app.py            ->  http://127.0.0.1:8080
"""
from __future__ import annotations

import asyncio
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import threading
import time
import uuid
import wave
from datetime import datetime
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import descargar_modelo
import dictado
import llm

BASE = Path(__file__).resolve().parent
DIR_MODELO = BASE / "modelo"
DIR_VOCES = BASE / "voces"
DIR_SALIDAS = BASE / "salidas"
DIR_GUIONES = BASE / "guiones"
DIR_TMP = BASE / ".tmp"
DIR_ESTATICO = BASE / "static"
ARCHIVO_CONFIG = BASE / "config.json"

for _d in (DIR_VOCES, DIR_SALIDAS, DIR_GUIONES, DIR_TMP, DIR_ESTATICO):
    _d.mkdir(exist_ok=True)

SIN_VENTANA = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Solo una síntesis a la vez: dos procesos llama-tts simultáneos duplican el
# consumo de VRAM y agotan las gráficas de 8 GB.
CERROJO_SINTESIS = threading.Lock()

MAX_BYTES_REFERENCIA = 60 * 1024 * 1024   # 60 MB de audio de referencia

# Idiomas admitidos por --tts-lang (ISO 639-1)
IDIOMAS = {
    "es": "Español",
    "en": "Inglés",
    "zh": "Chino",
    "de": "Alemán",
    "it": "Italiano",
    "pt": "Portugués",
    "ja": "Japonés",
    "ko": "Coreano",
    "fr": "Francés",
    "ru": "Ruso",
}

CONFIG_POR_DEFECTO: dict[str, Any] = {
    "binario": "",           # ruta a llama-tts.exe ("" = autodetección)
    "modelo": "",            # ruta al .gguf principal ("" = autodetección en ./modelo)
    "mmproj": "",            # ruta al mmproj .gguf
    "dispositivo": "auto",   # auto | cpu | <id de dispositivo, p.ej. Vulkan1>
    "capas_gpu": 99,
    "hilos": 0,              # 0 = automático
    "idioma": "es",
    "top_k": 40,
    "top_p": 0.95,
    "temp": 0.8,
    "semilla": -1,           # -1 = aleatoria
    "max_frames": 1200,      # -n : el modelo genera 12 frames por segundo de audio
    "chars_por_bloque": 280,
    "pausa_ms": 150,         # silencio insertado entre bloques
    # --- modelo de texto (LLM) ---
    "llm_modelo": "",        # id del catálogo, p. ej. "ollama:llama3.1:8b"
    "llm_dispositivo": "auto",
    "llm_capas": 99,
    "llm_contexto": 8192,
    "llm_temp": 0.85,
    "llm_max_tokens": 2048,
    # Con 8 GB de VRAM no caben a la vez un LLM de 5 GB y el TTS: por defecto
    # el modelo de texto se descarga de la GPU justo antes de sintetizar.
    "liberar_llm": "auto",   # auto | siempre | nunca
    "pausa_segmento_ms": 420,  # silencio entre intervenciones de un guion
    # --- dictado por micrófono (voz a texto) ---
    "stt_modelo": "small",
    "stt_dispositivo": "auto",   # auto | cuda | cpu
    "stt_idioma": "es",          # "auto" para detectarlo
    "stt_comandos": True,        # «abre interrogación», «coma», «punto»…
    "stt_pulir_llm": False,      # repasar los signos con el modelo de texto
    "dictado_silencio_ms": 1200,  # pausa que da por terminada la frase
    "dictado_sensibilidad": 2.5,  # veces por encima del ruido de fondo
    "dictado_autogenerar": True,  # sintetizar en cuanto se transcribe
    "dictado_continuo": True,     # volver a escuchar tras generar
}


# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------
def cargar_config() -> dict[str, Any]:
    cfg = dict(CONFIG_POR_DEFECTO)
    if ARCHIVO_CONFIG.exists():
        try:
            guardada = json.loads(ARCHIVO_CONFIG.read_text("utf-8"))
            cfg.update({k: v for k, v in guardada.items() if k in CONFIG_POR_DEFECTO})
        except (OSError, ValueError):
            pass
    return cfg


def guardar_config(cfg: dict[str, Any]) -> None:
    ARCHIVO_CONFIG.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), "utf-8")


# ---------------------------------------------------------------------------
# Detección del entorno
# ---------------------------------------------------------------------------
def buscar_binario() -> str:
    """Localiza llama-tts en el PATH o en las rutas de instalación habituales."""
    en_path = shutil.which("llama-tts") or shutil.which("llama-tts.exe")
    if en_path:
        return str(Path(en_path).resolve())

    local = Path(os.environ.get("LOCALAPPDATA", ""))
    candidatos = [
        # Windows (winget)
        local / "Microsoft/WinGet/Packages"
        / "ggml.llamacpp_Microsoft.Winget.Source_8wekyb3d8bbwe/llama-tts.exe",
        BASE / "llamacpp" / "llama-tts.exe",
        BASE / "llama.cpp" / "llama-tts.exe",
        Path("C:/llama.cpp/llama-tts.exe"),
        # macOS (Homebrew) y Linux
        Path("/opt/homebrew/bin/llama-tts"),
        Path("/usr/local/bin/llama-tts"),
        Path("/usr/bin/llama-tts"),
        BASE / "llama.cpp" / "build" / "bin" / "llama-tts",
    ]
    for c in candidatos:
        if c.is_file():
            return str(c)

    raiz = local / "Microsoft/WinGet/Packages"
    if raiz.is_dir():
        for hallado in raiz.glob("*/llama-tts.exe"):
            return str(hallado)
    return ""


def buscar_modelos() -> tuple[str, str]:
    """Devuelve (modelo, mmproj) a partir de los .gguf presentes en ./modelo."""
    if not DIR_MODELO.is_dir():
        return "", ""
    ggufs = sorted(DIR_MODELO.glob("*.gguf"))
    mmproj = next((g for g in ggufs if g.name.lower().startswith("mmproj")), None)
    modelo = next((g for g in ggufs if not g.name.lower().startswith("mmproj")), None)
    return (str(modelo) if modelo else "", str(mmproj) if mmproj else "")


def rutas_efectivas(cfg: dict[str, Any]) -> tuple[str, str, str]:
    binario = cfg.get("binario") or buscar_binario()
    modelo = cfg.get("modelo") or ""
    mmproj = cfg.get("mmproj") or ""
    if not modelo or not mmproj:
        auto_modelo, auto_mmproj = buscar_modelos()
        modelo = modelo or auto_modelo
        mmproj = mmproj or auto_mmproj
    return binario, modelo, mmproj


def _ejecutar(cmd: list[str], timeout: int = 30) -> str:
    try:
        r = subprocess.run(
            cmd, capture_output=True, timeout=timeout, text=True,
            encoding="utf-8", errors="replace", creationflags=SIN_VENTANA,
        )
        return (r.stdout or "") + (r.stderr or "")
    except (OSError, subprocess.SubprocessError):
        return ""


_cache: dict[str, Any] = {}


def listar_dispositivos(binario: str) -> list[dict[str, Any]]:
    """Enumera los aceleradores que ve llama.cpp (Vulkan, CUDA, ...)."""
    if not binario or not Path(binario).is_file():
        return []
    if binario in _cache:
        return _cache[binario]

    patron = re.compile(
        r"^\s*([A-Za-z]+\d+):\s+(.+?)\s+\((\d+)\s*MiB,\s*(\d+)\s*MiB free\)\s*$"
    )
    dispositivos: list[dict[str, Any]] = []
    for linea in _ejecutar([binario, "--list-devices"]).splitlines():
        m = patron.match(linea)
        if m:
            dispositivos.append({
                "id": m.group(1),
                "nombre": m.group(2).strip(),
                "memoria_mb": int(m.group(3)),
                "libre_mb": int(m.group(4)),
            })
    _cache[binario] = dispositivos
    return dispositivos


def dispositivo_preferido(binario: str) -> str:
    """Elige la mejor GPU disponible. Prioriza la dedicada sobre la integrada."""
    dispositivos = listar_dispositivos(binario)
    if not dispositivos:
        return ""
    dedicadas = ("nvidia", "geforce", "rtx", "quadro", "tesla", "radeon rx", "arc")
    for d in dispositivos:
        if any(marca in d["nombre"].lower() for marca in dedicadas):
            return d["id"]
    return max(dispositivos, key=lambda d: d["libre_mb"])["id"]


def version_binario(binario: str) -> str:
    """Cacheada: cada consulta lanza el binario y eso cuesta cientos de ms."""
    if not binario or not Path(binario).is_file():
        return ""
    clave = f"version::{binario}"
    if clave not in _cache:
        m = re.search(r"version:\s*(\S+.*)",
                      _ejecutar([binario, "--version"], timeout=20))
        _cache[clave] = m.group(1).strip() if m else ""
    return _cache[clave]


def soporta_qwen3tts(binario: str) -> bool:
    """Qwen3-TTS exige un llama-tts que acepte --mmproj (build >= b10000 aprox.)."""
    if not binario or not Path(binario).is_file():
        return False
    clave = f"help::{binario}"
    if clave not in _cache:
        _cache[clave] = "--mmproj" in _ejecutar([binario, "--help"], timeout=25)
    return _cache[clave]


def hay_ffmpeg() -> bool:
    return bool(shutil.which("ffmpeg"))


# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------
def convertir_a_wav(origen: Path, destino: Path, hz: int = 16000) -> None:
    """Normaliza cualquier audio a WAV PCM mono, que es lo que espera el modelo."""
    if not hay_ffmpeg():
        if origen.suffix.lower() in (".wav", ".mp3"):
            shutil.copyfile(origen, destino)
            return
        raise HTTPException(500, "Se necesita ffmpeg para convertir este formato de audio.")

    r = subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-i", str(origen), "-ac", "1", "-ar", str(hz),
         "-c:a", "pcm_s16le", str(destino)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=SIN_VENTANA,
    )
    if r.returncode != 0 or not destino.exists():
        raise HTTPException(500, f"ffmpeg no pudo convertir el audio: {r.stderr[:400]}")


def duracion_wav(ruta: Path) -> float:
    try:
        with wave.open(str(ruta), "rb") as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except (OSError, wave.Error):
        return 0.0


def unir_con_tiempos(partes: list[Path], destino: Path,
                     pausas_ms: list[int]) -> list[tuple[float, float]]:
    """Une los WAV aplicando una pausa después de cada uno y devuelve sus tiempos.

    `pausas_ms[i]` es el silencio que va detrás de `partes[i]` (el último se
    ignora). El resultado es la lista de (inicio, fin) de cada parte en segundos,
    que es justo lo que hace falta para escribir los subtítulos.
    """
    with wave.open(str(partes[0]), "rb") as w0:
        parametros = w0.getparams()
    bytes_por_frame = parametros.nchannels * parametros.sampwidth
    tramo = parametros.framerate

    tiempos: list[tuple[float, float]] = []
    frames_escritos = 0
    with wave.open(str(destino), "wb") as salida:
        salida.setparams(parametros)
        for i, parte in enumerate(partes):
            inicio = frames_escritos / tramo
            with wave.open(str(parte), "rb") as w:
                datos = w.readframes(w.getnframes())
                salida.writeframes(datos)
                frames_escritos += w.getnframes()
            tiempos.append((inicio, frames_escritos / tramo))

            if i < len(partes) - 1:
                n_silencio = int(tramo * pausas_ms[i] / 1000)
                if n_silencio > 0:
                    salida.writeframes(b"\x00" * (n_silencio * bytes_por_frame))
                    frames_escritos += n_silencio
    return tiempos


def a_srt(segmentos: list[dict[str, Any]], tiempos: list[tuple[float, float]]) -> str:
    """Genera subtítulos SRT a partir de los segmentos y sus tiempos."""
    def marca(s: float) -> str:
        h, resto = divmod(s, 3600)
        m, seg = divmod(resto, 60)
        return f"{int(h):02d}:{int(m):02d}:{int(seg):02d},{int(seg % 1 * 1000):03d}"

    lineas = []
    for i, (seg, (ini, fin)) in enumerate(zip(segmentos, tiempos), start=1):
        lineas.append(f"{i}\n{marca(ini)} --> {marca(fin)}\n"
                      f"{seg.get('personaje', '')}: {seg.get('texto', '')}\n")
    return "\n".join(lineas)


def unir_wavs(partes: list[Path], destino: Path, pausa_ms: int = 150) -> None:
    """Concatena WAVs del mismo formato intercalando un breve silencio."""
    partes = [p for p in partes if p.exists() and p.stat().st_size > 44]
    if not partes:
        raise RuntimeError("El modelo no generó ningún audio.")
    if len(partes) == 1:
        shutil.copyfile(partes[0], destino)
        return

    with wave.open(str(partes[0]), "rb") as w0:
        parametros = w0.getparams()
    silencio = b"\x00" * (
        int(parametros.framerate * pausa_ms / 1000)
        * parametros.nchannels * parametros.sampwidth
    )
    with wave.open(str(destino), "wb") as salida:
        salida.setparams(parametros)
        for i, parte in enumerate(partes):
            with wave.open(str(parte), "rb") as w:
                salida.writeframes(w.readframes(w.getnframes()))
            if i < len(partes) - 1 and silencio:
                salida.writeframes(silencio)


# ---------------------------------------------------------------------------
# Troceado del texto
# ---------------------------------------------------------------------------
def trocear_texto(texto: str, maximo: int) -> list[str]:
    """Parte el texto en bloques por frases para no agotar el límite de frames."""
    texto = re.sub(r"\s+", " ", texto).strip()
    if not texto:
        return []
    if len(texto) <= maximo:
        return [texto]

    bloques: list[str] = []
    actual = ""
    for frase in re.split(r"(?<=[.!?…。！？;:])\s+", texto):
        while len(frase) > maximo:  # frase gigantesca: cortar por coma o espacio
            corte = max(frase.rfind(",", 0, maximo), frase.rfind(" ", 0, maximo))
            if corte <= maximo // 2:
                corte = maximo
            if actual:
                bloques.append(actual.strip())
                actual = ""
            bloques.append(frase[:corte].strip())
            frase = frase[corte:].strip()
        if len(actual) + len(frase) + 1 <= maximo:
            actual = f"{actual} {frase}".strip()
        else:
            if actual:
                bloques.append(actual.strip())
            actual = frase
    if actual:
        bloques.append(actual.strip())
    return [b for b in bloques if b]


# ---------------------------------------------------------------------------
# Biblioteca de voces
# ---------------------------------------------------------------------------
def meta_voz(carpeta: Path) -> dict[str, Any] | None:
    meta, audio = carpeta / "voz.json", carpeta / "referencia.wav"
    if not meta.is_file() or not audio.is_file():
        return None
    try:
        datos = json.loads(meta.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    datos["id"] = carpeta.name
    datos["duracion"] = round(duracion_wav(audio), 2)
    return datos


def listar_voces() -> list[dict[str, Any]]:
    voces = [meta_voz(c) for c in DIR_VOCES.iterdir() if c.is_dir()]
    return sorted((v for v in voces if v), key=lambda v: v.get("creada", ""), reverse=True)


# ---------------------------------------------------------------------------
# Tareas de síntesis
# ---------------------------------------------------------------------------
class Tarea:
    def __init__(self, id_: str, total_bloques: int) -> None:
        self.id = id_
        self.eventos: queue.Queue[dict[str, Any]] = queue.Queue()
        self.estado = "en_curso"
        self.proceso: subprocess.Popen[str] | None = None
        self.cancelada = False
        self.total_bloques = total_bloques
        self.fin: float | None = None

    def emitir(self, tipo: str, **datos: Any) -> None:
        self.eventos.put({"tipo": tipo, **datos})
        if tipo in ("fin", "error", "cancelada"):
            self.fin = time.time()


TAREAS: dict[str, Tarea] = {}


def podar_tareas(vida_segundos: int = 900) -> None:
    """Descarta las tareas acabadas hace rato: si no, TAREAS crece sin límite."""
    ahora = time.time()
    for id_, t in list(TAREAS.items()):
        if t.fin and ahora - t.fin > vida_segundos:
            TAREAS.pop(id_, None)


def construir_comando(cfg: dict[str, Any], texto: str, salida: Path,
                      ref: Path | None, dispositivo: str) -> list[str]:
    """`dispositivo` vacío = CPU pura; en otro caso, el id que reporta llama.cpp."""
    binario, modelo, mmproj = rutas_efectivas(cfg)
    cmd = [
        binario,
        "-m", modelo,
        "-mm", mmproj,
        "-p", texto,
        "-o", str(salida),
        "--tts-lang", str(cfg["idioma"]),
        "-n", str(int(cfg["max_frames"])),
        "--top-k", str(int(cfg["top_k"])),
        "--top-p", str(float(cfg["top_p"])),
        "--temp", str(float(cfg["temp"])),
    ]
    if dispositivo:
        # -mmdev es decisivo: sin él el vocoder corre en CPU y tarda ~15x más.
        cmd += ["-ngl", str(int(cfg["capas_gpu"])),
                "--device", dispositivo,
                "-mmdev", dispositivo]
    else:
        cmd += ["-ngl", "0", "--device", "none", "--no-mmproj-offload"]
    if int(cfg.get("hilos") or 0) > 0:
        cmd += ["-t", str(int(cfg["hilos"]))]
    if int(cfg.get("semilla", -1)) >= 0:
        cmd += ["-s", str(int(cfg["semilla"]))]
    if ref is not None:
        cmd += ["--tts-speaker-file", str(ref)]
    return cmd


def ejecutar_sintesis(tarea: Tarea, cfg: dict[str, Any], bloques: list[str],
                      ref: Path | None, dispositivo: str, destino: Path) -> None:
    """Hilo de trabajo: sintetiza bloque a bloque y une los resultados."""
    partes: list[Path] = []
    inicio = time.time()
    liberar_llm_si_procede(cfg, dispositivo, tarea)
    if not CERROJO_SINTESIS.acquire(blocking=False):
        tarea.emitir("log", linea="Hay otra síntesis en curso; esperando turno…")
        CERROJO_SINTESIS.acquire()
    try:
        for i, bloque in enumerate(bloques, start=1):
            if tarea.cancelada:
                break
            parcial = DIR_TMP / f"{tarea.id}_{i:03d}.wav"
            tarea.emitir("bloque", indice=i, total=len(bloques), texto=bloque)

            cmd = construir_comando(cfg, bloque, parcial, ref, dispositivo)
            tarea.proceso = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                creationflags=SIN_VENTANA,
            )
            assert tarea.proceso.stdout is not None
            for linea in tarea.proceso.stdout:
                linea = linea.rstrip()
                if linea:
                    tarea.emitir("log", linea=linea)
            codigo = tarea.proceso.wait()
            tarea.proceso = None

            if tarea.cancelada:
                break
            if codigo != 0 or not parcial.exists():
                raise RuntimeError(f"llama-tts terminó con código {codigo} en el bloque {i}.")
            partes.append(parcial)

        if tarea.cancelada:
            tarea.estado = "cancelada"
            tarea.emitir("cancelada")
            return

        unir_wavs(partes, destino, int(cfg["pausa_ms"]))
        tarea.estado = "terminada"
        tarea.emitir("fin", archivo=destino.name,
                     duracion=round(duracion_wav(destino), 2),
                     segundos=round(time.time() - inicio, 1))
    except Exception as exc:  # noqa: BLE001 — el mensaje se muestra íntegro en la web
        tarea.estado = "error"
        tarea.emitir("error", mensaje=str(exc))
    finally:
        CERROJO_SINTESIS.release()
        for p in partes:
            p.unlink(missing_ok=True)
        podar_tareas()


def liberar_llm_si_procede(cfg: dict[str, Any], dispositivo: str,
                           tarea: "Tarea | None" = None) -> None:
    """Descarga el modelo de texto de la GPU antes de sintetizar.

    En una tarjeta de 8 GB no caben a la vez un LLM de 5 GB y el modelo de voz:
    si ambos van a la misma GPU, llama-tts se queda sin VRAM. Con "auto" solo se
    libera cuando los dos comparten GPU.
    """
    modo = str(cfg.get("liberar_llm", "auto"))
    if modo == "nunca" or not llm.SERVIDOR.vivo:
        return
    comparten_gpu = bool(dispositivo) and llm.SERVIDOR.dispositivo != "CPU"
    if modo == "siempre" or (modo == "auto" and comparten_gpu):
        if tarea:
            tarea.emitir("log", linea=f"Liberando '{llm.SERVIDOR.nombre}' de la GPU "
                                      f"para dejar sitio al modelo de voz…")
        llm.SERVIDOR.parar()


def ejecutar_guion(tarea: Tarea, cfg: dict[str, Any], segmentos: list[dict[str, Any]],
                   dispositivo: str, destino: Path) -> None:
    """Sintetiza un guion completo, cada intervención con la voz de su personaje."""
    audios_segmento: list[Path] = []
    temporales: list[Path] = []
    inicio = time.time()

    liberar_llm_si_procede(cfg, dispositivo, tarea)
    if not CERROJO_SINTESIS.acquire(blocking=False):
        tarea.emitir("log", linea="Hay otra síntesis en curso; esperando turno…")
        CERROJO_SINTESIS.acquire()
    try:
        for i, seg in enumerate(segmentos, start=1):
            if tarea.cancelada:
                break
            personaje = seg.get("personaje", "NARRADOR")
            texto = str(seg.get("texto", "")).strip()
            if not texto:
                continue

            ref: Path | None = None
            if seg.get("voz"):
                candidata = DIR_VOCES / Path(str(seg["voz"])).name / "referencia.wav"
                ref = candidata if candidata.is_file() else None

            tarea.emitir("bloque", indice=i, total=len(segmentos),
                         texto=texto, personaje=personaje)

            trozos: list[Path] = []
            for j, bloque in enumerate(trocear_texto(texto, int(cfg["chars_por_bloque"])), 1):
                if tarea.cancelada:
                    break
                parcial = DIR_TMP / f"{tarea.id}_s{i:03d}_{j:03d}.wav"
                temporales.append(parcial)
                cmd = construir_comando(cfg, bloque, parcial, ref, dispositivo)
                tarea.proceso = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                    encoding="utf-8", errors="replace", bufsize=1,
                    creationflags=SIN_VENTANA)
                assert tarea.proceso.stdout is not None
                for linea in tarea.proceso.stdout:
                    linea = linea.rstrip()
                    if linea:
                        tarea.emitir("log", linea=linea)
                codigo = tarea.proceso.wait()
                tarea.proceso = None
                if tarea.cancelada:
                    break
                if codigo != 0 or not parcial.exists():
                    raise RuntimeError(
                        f"llama-tts falló (código {codigo}) en la intervención {i} "
                        f"de {personaje}.")
                trozos.append(parcial)

            if tarea.cancelada:
                break
            audio_seg = DIR_TMP / f"{tarea.id}_seg{i:03d}.wav"
            temporales.append(audio_seg)
            unir_wavs(trozos, audio_seg, int(cfg["pausa_ms"]))
            audios_segmento.append(audio_seg)

        if tarea.cancelada:
            tarea.estado = "cancelada"
            tarea.emitir("cancelada")
            return
        if not audios_segmento:
            raise RuntimeError("El guion no tiene ninguna intervención con texto.")

        usados = [s for s in segmentos if str(s.get("texto", "")).strip()]
        pausas = [int(s.get("pausa_ms") or cfg["pausa_segmento_ms"]) for s in usados]
        tiempos = unir_con_tiempos(audios_segmento, destino, pausas)

        # Los subtítulos se guardan junto al WAV, con el mismo nombre
        destino.with_suffix(".srt").write_text(a_srt(usados, tiempos), "utf-8")

        tarea.estado = "terminada"
        tarea.emitir("fin", archivo=destino.name,
                     duracion=round(duracion_wav(destino), 2),
                     segundos=round(time.time() - inicio, 1),
                     srt=destino.with_suffix(".srt").name,
                     tiempos=[{"personaje": s.get("personaje", ""),
                               "texto": s.get("texto", ""),
                               "inicio": round(t[0], 2), "fin": round(t[1], 2)}
                              for s, t in zip(usados, tiempos)])
    except Exception as exc:  # noqa: BLE001 — el mensaje se muestra íntegro en la web
        tarea.estado = "error"
        tarea.emitir("error", mensaje=str(exc))
    finally:
        CERROJO_SINTESIS.release()
        for p in temporales:
            p.unlink(missing_ok=True)
        podar_tareas()


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
app = FastAPI(title="Clonador de voz local")


@app.get("/")
def raiz() -> FileResponse:
    return FileResponse(DIR_ESTATICO / "index.html")


@app.get("/api/estado")
def estado() -> dict[str, Any]:
    cfg = cargar_config()
    binario, modelo, mmproj = rutas_efectivas(cfg)
    dispositivos = listar_dispositivos(binario)
    return {
        "binario": binario,
        "binario_ok": bool(binario and Path(binario).is_file()),
        "version": version_binario(binario),
        "soporta_qwen3tts": soporta_qwen3tts(binario),
        "modelo": modelo,
        "modelo_ok": bool(modelo and Path(modelo).is_file()),
        "mmproj": mmproj,
        "mmproj_ok": bool(mmproj and Path(mmproj).is_file()),
        "dispositivos": dispositivos,
        "dispositivo_preferido": dispositivo_preferido(binario),
        "ffmpeg": hay_ffmpeg(),
        "idiomas": IDIOMAS,
        "cpus": os.cpu_count() or 4,
        "config": cfg,
    }


@app.get("/api/config")
def obtener_config() -> dict[str, Any]:
    return cargar_config()


@app.post("/api/config")
async def actualizar_config(datos: dict[str, Any]) -> dict[str, Any]:
    cfg = cargar_config()
    for clave, valor in datos.items():
        if clave in CONFIG_POR_DEFECTO:
            cfg[clave] = valor
    guardar_config(cfg)
    _cache.clear()
    return cfg


# ---------------------------------------------------------------------------
# Descarga de los modelos desde Hugging Face
# ---------------------------------------------------------------------------
class DescargaModelo:
    """Estado compartido de la (única) descarga de modelos en curso."""

    def __init__(self) -> None:
        self.activa = False
        self.cancelada = False
        self.error = ""
        self.terminada = False
        self.archivo = ""
        self.hecho = 0
        self.total = 0
        self.velocidad = 0.0

    def progreso(self, archivo: str, hecho: int, total: int, velocidad: float) -> None:
        self.archivo, self.hecho, self.total, self.velocidad = archivo, hecho, total, velocidad

    def instantanea(self) -> dict[str, Any]:
        return {
            "activa": self.activa, "terminada": self.terminada, "error": self.error,
            "archivo": self.archivo, "hecho": self.hecho, "total": self.total,
            "velocidad": round(self.velocidad),
        }


DESCARGA = DescargaModelo()


@app.get("/api/modelo/catalogo")
def catalogo_modelos() -> dict[str, Any]:
    return {
        "repo": descargar_modelo.REPO,
        "url": f"https://huggingface.co/{descargar_modelo.REPO}",
        "catalogo": descargar_modelo.CATALOGO,
        "por_defecto": descargar_modelo.POR_DEFECTO,
        "falta": descargar_modelo.falta_algo(DIR_MODELO),
    }


@app.post("/api/modelo/descargar")
def iniciar_descarga(datos: dict[str, Any]) -> dict[str, Any]:
    if DESCARGA.activa:
        raise HTTPException(409, "Ya hay una descarga en curso.")

    clave_modelo = str(datos.get("modelo") or descargar_modelo.POR_DEFECTO["modelo"])
    clave_mmproj = str(datos.get("mmproj") or descargar_modelo.POR_DEFECTO["mmproj"])
    if (clave_modelo not in descargar_modelo.CATALOGO["modelo"]
            or clave_mmproj not in descargar_modelo.CATALOGO["mmproj"]):
        raise HTTPException(400, "Cuantización desconocida.")

    DESCARGA.__init__()  # reinicia el estado
    DESCARGA.activa = True

    def trabajo() -> None:
        try:
            descargar_modelo.descargar_todo(
                clave_modelo, clave_mmproj, DIR_MODELO, DESCARGA.progreso,
                verificar=True, cancelado=lambda: DESCARGA.cancelada)
            DESCARGA.terminada = True
            _cache.clear()
        except Exception as exc:  # noqa: BLE001 — se enseña tal cual en la web
            DESCARGA.error = str(exc)
        finally:
            DESCARGA.activa = False

    threading.Thread(target=trabajo, daemon=True).start()
    return {"ok": True, "modelo": clave_modelo, "mmproj": clave_mmproj}


@app.post("/api/modelo/cancelar")
def cancelar_descarga() -> dict[str, bool]:
    DESCARGA.cancelada = True
    return {"ok": True}


@app.get("/api/modelo/progreso")
async def progreso_descarga() -> StreamingResponse:
    async def flujo():
        while True:
            yield f"data: {json.dumps(DESCARGA.instantanea())}\n\n"
            if not DESCARGA.activa:
                return
            await asyncio.sleep(0.4)

    return StreamingResponse(flujo(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------------------
# Dictado por micrófono (voz a texto)
# ---------------------------------------------------------------------------
@app.get("/api/dictado/estado")
def dictado_estado() -> dict[str, Any]:
    cfg = cargar_config()
    return {**dictado.TRANSCRIPTOR.instantanea(),
            "config": {c: cfg[c] for c in CONFIG_POR_DEFECTO
                       if c.startswith(("stt_", "dictado_"))}}


@app.post("/api/dictado/cargar")
def dictado_cargar(datos: dict[str, Any]) -> dict[str, Any]:
    cfg = cargar_config()
    for corto, clave in (("modelo", "stt_modelo"), ("dispositivo", "stt_dispositivo")):
        if datos.get(corto) not in (None, ""):
            cfg[clave] = datos[corto]
    guardar_config(cfg)

    if not dictado.TRANSCRIPTOR.cargar(str(cfg["stt_modelo"]), str(cfg["stt_dispositivo"])):
        raise HTTPException(400, dictado.TRANSCRIPTOR.error or
                            "No se pudo cargar el modelo de dictado.")
    return dictado.TRANSCRIPTOR.instantanea()


@app.post("/api/dictado/parar")
def dictado_parar() -> dict[str, bool]:
    dictado.TRANSCRIPTOR.parar()
    return {"ok": True}


@app.post("/api/dictado/transcribir")
async def dictado_transcribir(audio: UploadFile = File(...)) -> dict[str, Any]:
    cfg = cargar_config()
    if dictado.TRANSCRIPTOR.modelo is None:
        if not dictado.TRANSCRIPTOR.cargar(str(cfg["stt_modelo"]),
                                           str(cfg["stt_dispositivo"])):
            raise HTTPException(400, dictado.TRANSCRIPTOR.error or
                                "No se pudo cargar el modelo de dictado.")

    datos_audio = await audio.read()
    if len(datos_audio) > MAX_BYTES_REFERENCIA:
        raise HTTPException(413, "El audio dictado es demasiado largo.")

    sufijo = Path(audio.filename or "dictado.webm").suffix or ".webm"
    bruto = DIR_TMP / f"dictado-{uuid.uuid4().hex[:10]}{sufijo}"
    onda = bruto.with_suffix(".wav")
    try:
        bruto.write_bytes(datos_audio)
        # Whisper quiere 16 kHz mono; el navegador graba WebM/Opus
        convertir_a_wav(bruto, onda)
        resultado = dictado.TRANSCRIPTOR.transcribir(
            onda, str(cfg["stt_idioma"]), bool(cfg["stt_comandos"]))

        # Repaso opcional de los signos con el modelo de texto ya cargado
        if cfg.get("stt_pulir_llm") and llm.SERVIDOR.vivo:
            pulido = dictado.pulir_con_llm(resultado["texto"], llm.SERVIDOR)
            resultado["pulido"] = pulido != resultado["texto"]
            resultado["texto"] = pulido
        return resultado
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — el mensaje se enseña en la web
        raise HTTPException(500, f"No se pudo transcribir: {exc}") from exc
    finally:
        bruto.unlink(missing_ok=True)
        onda.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Modelos de texto (LLM) servidos con llama-server
# ---------------------------------------------------------------------------
def binario_hermano(binario_tts: str, nombre: str) -> str:
    """llama-server y llama-cli viven junto a llama-tts."""
    if not binario_tts:
        return ""
    carpeta = Path(binario_tts).parent
    for candidato in (carpeta / f"{nombre}.exe", carpeta / nombre):
        if candidato.is_file():
            return str(candidato)
    return shutil.which(nombre) or ""


def resolver_dispositivo(binario: str, modo: str) -> str:
    """Devuelve el id del dispositivo, o cadena vacía para CPU pura."""
    if modo == "cpu":
        return ""
    if modo == "auto":
        return dispositivo_preferido(binario)
    return modo


@app.get("/api/llm/modelos")
def llm_modelos() -> dict[str, Any]:
    return {
        "modelos": llm.catalogo(),
        "carpeta_ollama": str(llm.carpeta_ollama() or ""),
        "carpeta_propia": str(llm.DIR_MODELOS_TEXTO),
    }


@app.get("/api/llm/estado")
def llm_estado() -> dict[str, Any]:
    return llm.SERVIDOR.instantanea()


@app.post("/api/llm/comprobar")
def llm_comprobar(datos: dict[str, Any]) -> dict[str, Any]:
    binario, _, _ = rutas_efectivas(cargar_config())
    cli = binario_hermano(binario, "llama-cli")
    if not cli:
        raise HTTPException(400, "No se encuentra llama-cli junto a llama-tts.")
    return llm.comprobar(str(datos.get("modelo", "")), cli)


@app.post("/api/llm/cargar")
def llm_cargar(datos: dict[str, Any]) -> dict[str, Any]:
    cfg = cargar_config()
    for clave in ("llm_modelo", "llm_dispositivo", "llm_capas", "llm_contexto"):
        corto = clave.removeprefix("llm_")
        if datos.get(corto) not in (None, ""):
            cfg[clave] = datos[corto]
    guardar_config(cfg)

    binario, _, _ = rutas_efectivas(cfg)
    servidor = binario_hermano(binario, "llama-server")
    if not servidor:
        raise HTTPException(400, "No se encuentra llama-server junto a llama-tts.")

    modelo = llm.por_id(str(cfg["llm_modelo"]))
    if not modelo:
        raise HTTPException(400, "Ese modelo de texto ya no está disponible.")
    if modelo.get("es_embedding"):
        raise HTTPException(400, f"'{modelo['nombre']}' es un modelo de embeddings: "
                                 "sirve para buscar, no para escribir texto.")

    dispositivo = resolver_dispositivo(binario, str(cfg["llm_dispositivo"]))
    ok = llm.SERVIDOR.arrancar(servidor, modelo, dispositivo,
                               int(cfg["llm_capas"]), int(cfg["llm_contexto"]))
    if not ok:
        raise HTTPException(400, llm.SERVIDOR.error or "No se pudo cargar el modelo.")
    return llm.SERVIDOR.instantanea()


@app.post("/api/llm/parar")
def llm_parar() -> dict[str, bool]:
    llm.SERVIDOR.parar()
    return {"ok": True}


@app.post("/api/llm/guion")
def llm_guion(datos: dict[str, Any]) -> dict[str, Any]:
    """Pide al modelo de texto que escriba un guion. Devuelve el id de la tarea."""
    cfg = cargar_config()
    idea = str(datos.get("idea", "")).strip()
    if not idea:
        raise HTTPException(400, "Cuéntale al modelo qué historia quieres.")
    if not llm.SERVIDOR.vivo:
        raise HTTPException(400, "Carga antes un modelo de texto.")

    mensajes = (llm.peticion_guion(
        idea,
        n_personajes=int(datos.get("personajes") or 0),
        palabras=int(datos.get("palabras") or 0),
        idioma=IDIOMAS.get(str(datos.get("idioma") or cfg["idioma"]), "español"),
    ) if not datos.get("continuar") else [
        {"role": "system", "content": llm.SISTEMA_GUION},
        {"role": "user", "content": str(datos.get("continuar"))},
    ])

    id_tarea = uuid.uuid4().hex[:12]
    tarea = Tarea(id_tarea, 1)
    TAREAS[id_tarea] = tarea

    def trabajo() -> None:
        completo = ""
        try:
            for trozo in llm.SERVIDOR.chat(
                    mensajes, temperatura=float(cfg["llm_temp"]),
                    max_tokens=int(cfg["llm_max_tokens"]),
                    cancelado=lambda: tarea.cancelada):
                completo += trozo
                tarea.emitir("texto", trozo=trozo)
            if tarea.cancelada:
                tarea.estado = "cancelada"
                tarea.emitir("cancelada")
                return
            segmentos = llm.analizar_guion(completo)
            tarea.estado = "terminada"
            tarea.emitir("fin", texto=llm.limpiar_razonamiento(completo),
                         segmentos=segmentos,
                         personajes=llm.personajes_de(segmentos))
        except Exception as exc:  # noqa: BLE001
            tarea.estado = "error"
            tarea.emitir("error", mensaje=str(exc))
        finally:
            podar_tareas()

    threading.Thread(target=trabajo, daemon=True).start()
    return {"id": id_tarea}


@app.post("/api/llm/analizar")
def llm_analizar(datos: dict[str, Any]) -> dict[str, Any]:
    """Convierte un texto pegado a mano en segmentos con personaje."""
    segmentos = llm.analizar_guion(str(datos.get("texto", "")))
    return {"segmentos": segmentos, "personajes": llm.personajes_de(segmentos)}


# ---------------------------------------------------------------------------
# Guiones multivoz
# ---------------------------------------------------------------------------
def ruta_guion(id_guion: str) -> Path:
    return DIR_GUIONES / f"{Path(id_guion).name}.json"


@app.get("/api/guiones")
def listar_guiones() -> list[dict[str, Any]]:
    guiones = []
    for archivo in DIR_GUIONES.glob("*.json"):
        try:
            g = json.loads(archivo.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        guiones.append({"id": g.get("id", archivo.stem),
                        "titulo": g.get("titulo", "Sin título"),
                        "creado": g.get("creado", ""),
                        "segmentos": len(g.get("segmentos", [])),
                        "personajes": llm.personajes_de(g.get("segmentos", []))})
    return sorted(guiones, key=lambda g: g["creado"], reverse=True)


@app.get("/api/guiones/{id_guion}")
def obtener_guion(id_guion: str) -> dict[str, Any]:
    archivo = ruta_guion(id_guion)
    if not archivo.is_file():
        raise HTTPException(404, "Guion no encontrado")
    return json.loads(archivo.read_text("utf-8"))


@app.post("/api/guiones")
def guardar_guion(datos: dict[str, Any]) -> dict[str, Any]:
    id_guion = str(datos.get("id") or uuid.uuid4().hex[:12])
    guion = {
        "id": id_guion,
        "titulo": str(datos.get("titulo") or "Guion sin título")[:120],
        "creado": datos.get("creado") or datetime.now().isoformat(timespec="seconds"),
        "actualizado": datetime.now().isoformat(timespec="seconds"),
        "idioma": datos.get("idioma", "es"),
        "segmentos": datos.get("segmentos", []),
    }
    ruta_guion(id_guion).write_text(
        json.dumps(guion, indent=2, ensure_ascii=False), "utf-8")
    return guion


@app.delete("/api/guiones/{id_guion}")
def borrar_guion(id_guion: str) -> dict[str, bool]:
    ruta_guion(id_guion).unlink(missing_ok=True)
    return {"ok": True}


@app.post("/api/guion/sintetizar")
def sintetizar_guion(datos: dict[str, Any]) -> dict[str, Any]:
    cfg = cargar_config()
    for clave in ("idioma", "top_k", "top_p", "temp", "semilla", "max_frames",
                  "chars_por_bloque", "pausa_ms", "pausa_segmento_ms",
                  "capas_gpu", "hilos"):
        if datos.get(clave) not in (None, ""):
            cfg[clave] = datos[clave]

    binario, modelo, mmproj = rutas_efectivas(cfg)
    for ruta, etiqueta in ((binario, "binario llama-tts"), (modelo, "modelo"),
                           (mmproj, "mmproj")):
        if not ruta or not Path(ruta).is_file():
            raise HTTPException(400, f"No se encuentra el {etiqueta}. Revisa los ajustes.")

    segmentos = datos.get("segmentos") or []
    if not any(str(s.get("texto", "")).strip() for s in segmentos):
        raise HTTPException(400, "El guion está vacío.")

    dispositivo = resolver_dispositivo(
        binario, str(datos.get("dispositivo") or cfg.get("dispositivo") or "auto"))

    id_tarea = uuid.uuid4().hex[:12]
    titulo = re.sub(r"[^\w\- ]", "", str(datos.get("titulo") or "guion")).strip()[:40]
    destino = DIR_SALIDAS / (f"{datetime.now():%Y%m%d-%H%M%S}-"
                             f"{titulo.replace(' ', '_') or 'guion'}-{id_tarea}.wav")

    tarea = Tarea(id_tarea, len(segmentos))
    TAREAS[id_tarea] = tarea
    threading.Thread(target=ejecutar_guion, daemon=True,
                     args=(tarea, cfg, segmentos, dispositivo, destino)).start()

    return {"id": id_tarea, "segmentos": len(segmentos),
            "dispositivo": dispositivo or "CPU", "archivo": destino.name}


@app.get("/api/voces")
def api_listar_voces() -> list[dict[str, Any]]:
    return listar_voces()


@app.post("/api/voces")
async def crear_voz(audio: UploadFile = File(...), nombre: str = Form(...),
                    transcripcion: str = Form("")) -> dict[str, Any]:
    id_voz = uuid.uuid4().hex[:12]
    carpeta = DIR_VOCES / id_voz
    carpeta.mkdir(parents=True)

    sufijo = Path(audio.filename or "audio.webm").suffix or ".webm"
    bruto = carpeta / f"original{sufijo}"
    try:
        datos_audio = await audio.read()
        if len(datos_audio) > MAX_BYTES_REFERENCIA:
            raise HTTPException(413, "El audio de referencia no puede pasar de 60 MB. "
                                     "Con 10-15 segundos de voz es más que suficiente.")
        bruto.write_bytes(datos_audio)
        convertir_a_wav(bruto, carpeta / "referencia.wav")
    except Exception:
        shutil.rmtree(carpeta, ignore_errors=True)
        raise
    bruto.unlink(missing_ok=True)

    (carpeta / "voz.json").write_text(json.dumps({
        "nombre": nombre.strip() or "Voz sin nombre",
        "transcripcion": transcripcion.strip(),
        "creada": datetime.now().isoformat(timespec="seconds"),
    }, indent=2, ensure_ascii=False), "utf-8")
    return meta_voz(carpeta) or {}


@app.delete("/api/voces/{id_voz}")
def borrar_voz(id_voz: str) -> dict[str, bool]:
    carpeta = DIR_VOCES / Path(id_voz).name
    if not carpeta.is_dir():
        raise HTTPException(404, "Voz no encontrada")
    shutil.rmtree(carpeta, ignore_errors=True)
    return {"ok": True}


@app.get("/api/voces/{id_voz}/audio")
def audio_voz(id_voz: str) -> FileResponse:
    ruta = DIR_VOCES / Path(id_voz).name / "referencia.wav"
    if not ruta.is_file():
        raise HTTPException(404, "Audio no encontrado")
    return FileResponse(ruta, media_type="audio/wav")


@app.post("/api/generar")
async def generar(datos: dict[str, Any]) -> dict[str, Any]:
    cfg = cargar_config()
    for clave in ("idioma", "top_k", "top_p", "temp", "semilla", "max_frames",
                  "chars_por_bloque", "pausa_ms", "capas_gpu", "hilos"):
        if datos.get(clave) not in (None, ""):
            cfg[clave] = datos[clave]

    binario, modelo, mmproj = rutas_efectivas(cfg)
    for ruta, etiqueta in ((binario, "binario llama-tts"), (modelo, "modelo"),
                           (mmproj, "mmproj")):
        if not ruta or not Path(ruta).is_file():
            raise HTTPException(400, f"No se encuentra el {etiqueta}. Revisa los ajustes.")
    if not soporta_qwen3tts(binario):
        raise HTTPException(400, "Tu llama.cpp no admite Qwen3-TTS (falta --mmproj en "
                                 "llama-tts). Actualízalo: winget upgrade ggml.llamacpp")

    texto = str(datos.get("texto", "")).strip()
    if not texto:
        raise HTTPException(400, "Escribe el texto que quieres sintetizar.")

    ref: Path | None = None
    if datos.get("voz"):
        ref = DIR_VOCES / Path(str(datos["voz"])).name / "referencia.wav"
        if not ref.is_file():
            raise HTTPException(400, "La voz de referencia no existe.")

    modo = str(datos.get("dispositivo") or cfg.get("dispositivo") or "auto")
    if modo == "cpu":
        dispositivo = ""
    elif modo == "auto":
        dispositivo = dispositivo_preferido(binario)
    else:
        dispositivo = modo

    bloques = trocear_texto(texto, int(cfg["chars_por_bloque"]))
    id_tarea = uuid.uuid4().hex[:12]
    destino = DIR_SALIDAS / f"{datetime.now():%Y%m%d-%H%M%S}-{id_tarea}.wav"

    tarea = Tarea(id_tarea, len(bloques))
    TAREAS[id_tarea] = tarea
    threading.Thread(target=ejecutar_sintesis, daemon=True,
                     args=(tarea, cfg, bloques, ref, dispositivo, destino)).start()

    return {"id": id_tarea, "bloques": len(bloques),
            "dispositivo": dispositivo or "CPU", "archivo": destino.name}


@app.get("/api/tarea/{id_tarea}/eventos")
async def eventos(id_tarea: str) -> StreamingResponse:
    tarea = TAREAS.get(id_tarea)
    if not tarea:
        raise HTTPException(404, "Tarea no encontrada")

    async def flujo():
        while True:
            try:
                evento = tarea.eventos.get_nowait()
            except queue.Empty:
                if tarea.estado != "en_curso":
                    break
                await asyncio.sleep(0.08)
                continue
            yield f"data: {json.dumps(evento, ensure_ascii=False)}\n\n"
            if evento["tipo"] in ("fin", "error", "cancelada"):
                return

    return StreamingResponse(flujo(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.post("/api/tarea/{id_tarea}/cancelar")
def cancelar(id_tarea: str) -> dict[str, bool]:
    tarea = TAREAS.get(id_tarea)
    if not tarea:
        raise HTTPException(404, "Tarea no encontrada")
    tarea.cancelada = True
    if tarea.proceso and tarea.proceso.poll() is None:
        tarea.proceso.kill()
    return {"ok": True}


@app.get("/api/salidas")
def listar_salidas() -> list[dict[str, Any]]:
    archivos = sorted(DIR_SALIDAS.glob("*.wav"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
    return [{
        "archivo": a.name,
        "tamano": a.stat().st_size,
        "duracion": round(duracion_wav(a), 2),
        "fecha": datetime.fromtimestamp(a.stat().st_mtime).isoformat(timespec="seconds"),
    } for a in archivos[:80]]


@app.get("/api/salidas/{archivo}")
def obtener_salida(archivo: str) -> FileResponse:
    ruta = DIR_SALIDAS / Path(archivo).name
    if not ruta.is_file():
        raise HTTPException(404, "Archivo no encontrado")
    tipo = "text/plain; charset=utf-8" if ruta.suffix == ".srt" else "audio/wav"
    return FileResponse(ruta, media_type=tipo, filename=ruta.name)


@app.delete("/api/salidas/{archivo}")
def borrar_salida(archivo: str) -> dict[str, bool]:
    ruta = DIR_SALIDAS / Path(archivo).name
    ruta.unlink(missing_ok=True)
    ruta.with_suffix(".srt").unlink(missing_ok=True)   # los subtítulos van con el audio
    return {"ok": True}


@app.on_event("shutdown")
def al_cerrar() -> None:
    """No dejar procesos ni modelos huérfanos ocupando VRAM."""
    llm.SERVIDOR.parar()
    dictado.TRANSCRIPTOR.parar()


app.mount("/static", StaticFiles(directory=DIR_ESTATICO), name="static")


def puerto_libre(preferido: int) -> int:
    """Devuelve el primer puerto libre a partir del preferido."""
    for puerto in range(preferido, preferido + 20):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", puerto)) != 0:
                return puerto
    return preferido


if __name__ == "__main__":
    puerto = puerto_libre(int(os.environ.get("PUERTO", "8080")))
    if descargar_modelo.falta_algo(DIR_MODELO):
        print("\n  [!] Faltan los modelos .gguf en la carpeta 'modelo'.")
        print("      Puedes descargarlos desde la propia web, o con:")
        print("      python descargar_modelo.py")
    print(f"\n  Clonador de voz local  ->  http://127.0.0.1:{puerto}\n")
    uvicorn.run(app, host="127.0.0.1", port=puerto, log_level="warning")
