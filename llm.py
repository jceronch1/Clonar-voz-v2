# -*- coding: utf-8 -*-
"""
Modelos de texto locales servidos con llama.cpp.

Descubre los modelos que ya tienes en Ollama (sus blobs son GGUF estándar) más
cualquier .gguf suelto en la carpeta `modelos-texto/`, y los sirve con
`llama-server`, hablándole por su API compatible con OpenAI.

Ojo: no todo lo que descarga Ollama carga en llama.cpp. Algunas variantes traen
metadatos que llama.cpp todavía no entiende (p. ej. `qwen35.rope.dimension_sections`)
y los modelos de embeddings no generan texto. Por eso cada modelo se puede
comprobar antes de usarlo y el error real se enseña tal cual.
"""
from __future__ import annotations

import json
import os
import re
import socket
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterator

BASE = Path(__file__).resolve().parent
DIR_MODELOS_TEXTO = BASE / "modelos-texto"
CACHE_COMPAT = BASE / ".compat-llm.json"
SIN_VENTANA = getattr(subprocess, "CREATE_NO_WINDOW", 0)

MEDIA_MODELO = "application/vnd.ollama.image.model"

# Tamaño en bytes de cada tipo escalar de GGUF
_ESCALARES = {0: ("B", 1), 1: ("b", 1), 2: ("H", 2), 3: ("h", 2), 4: ("I", 4),
              5: ("i", 4), 6: ("f", 4), 7: ("?", 1), 10: ("Q", 8), 11: ("q", 8),
              12: ("d", 8)}


# ---------------------------------------------------------------------------
# Lectura de metadatos GGUF
# ---------------------------------------------------------------------------
def leer_metadatos(ruta: Path) -> dict[str, Any]:
    """Lee la cabecera GGUF y devuelve arquitectura, contexto y si es de embeddings."""
    try:
        with open(ruta, "rb") as f:
            if f.read(4) != b"GGUF":
                return {}
            _version, _n_tensores, n_kv = struct.unpack("<IQQ", f.read(20))

            def cadena() -> str:
                (n,) = struct.unpack("<Q", f.read(8))
                return f.read(n).decode("utf-8", "replace")

            def valor(tipo: int) -> Any:
                if tipo == 8:
                    return cadena()
                if tipo == 9:                      # array
                    (t_elem,) = struct.unpack("<I", f.read(4))
                    (n,) = struct.unpack("<Q", f.read(8))
                    if t_elem in _ESCALARES:
                        f.seek(_ESCALARES[t_elem][1] * n, 1)
                    else:
                        for _ in range(n):         # array de cadenas: hay que recorrerlo
                            valor(t_elem)
                    return f"<{n} elementos>"
                fmt, tam = _ESCALARES[tipo]
                return struct.unpack("<" + fmt, f.read(tam))[0]

            datos: dict[str, Any] = {}
            for _ in range(n_kv):
                clave = cadena()
                (tipo,) = struct.unpack("<I", f.read(4))
                v = valor(tipo)
                if (clave in ("general.architecture", "general.name",
                              "tokenizer.chat_template")
                        or clave.endswith((".context_length", ".pooling_type"))):
                    datos[clave] = v

        arch = datos.get("general.architecture", "")
        return {
            "arquitectura": arch,
            "nombre_interno": datos.get("general.name", ""),
            "contexto": next((v for k, v in datos.items()
                              if k.endswith(".context_length")), 0),
            "plantilla_chat": bool(datos.get("tokenizer.chat_template")),
            # Los modelos de embeddings declaran pooling_type y no sirven para chatear
            "es_embedding": any(k.endswith(".pooling_type") for k in datos),
        }
    except (OSError, struct.error, KeyError, ValueError):
        return {}


# ---------------------------------------------------------------------------
# Descubrimiento de modelos
# ---------------------------------------------------------------------------
def carpeta_ollama() -> Path | None:
    for candidata in (os.environ.get("OLLAMA_MODELS"),
                      Path.home() / ".ollama" / "models",
                      Path("C:/Users/Public/.ollama/models")):
        if candidata and Path(candidata).is_dir():
            return Path(candidata)
    return None


def _nombre_ollama(manifiesto: Path, raiz: Path) -> str:
    partes = manifiesto.parts[len(raiz.parts) + 1:]
    if not partes:
        return manifiesto.name
    familia = partes[1:-1] if partes[0] == "hf.co" else partes[2:-1]
    return "/".join(familia) + ":" + partes[-1]


def descubrir_ollama() -> list[dict[str, Any]]:
    """Modelos de Ollama con pesos locales. Los ':cloud' no tienen y se descartan."""
    raiz = carpeta_ollama()
    if not raiz:
        return []

    encontrados: list[dict[str, Any]] = []
    for manifiesto in sorted((raiz / "manifests").glob("**/*")):
        if not manifiesto.is_file():
            continue
        try:
            datos = json.loads(manifiesto.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        capa = next((c for c in (datos.get("layers") or [])
                     if c.get("mediaType") == MEDIA_MODELO), None)
        if not capa:
            continue                                # modelo cloud: sin pesos locales
        blob = raiz / "blobs" / capa["digest"].replace(":", "-")
        if not blob.is_file():
            continue
        encontrados.append({
            "id": f"ollama:{_nombre_ollama(manifiesto, raiz)}",
            "nombre": _nombre_ollama(manifiesto, raiz),
            "origen": "Ollama",
            "ruta": str(blob),
            "bytes": capa["size"],
        })
    return encontrados


def descubrir_sueltos() -> list[dict[str, Any]]:
    """Cualquier .gguf que dejes en modelos-texto/."""
    if not DIR_MODELOS_TEXTO.is_dir():
        return []
    return [{
        "id": f"archivo:{g.name}",
        "nombre": g.stem,
        "origen": "Carpeta local",
        "ruta": str(g),
        "bytes": g.stat().st_size,
    } for g in sorted(DIR_MODELOS_TEXTO.glob("*.gguf"))]


_cache_meta: dict[str, dict] = {}


def catalogo() -> list[dict[str, Any]]:
    """Lista completa, con metadatos y el resultado de compatibilidad si se probó."""
    compat = _cargar_compat()
    modelos = descubrir_ollama() + descubrir_sueltos()
    for m in modelos:
        clave = m["ruta"]
        if clave not in _cache_meta:
            _cache_meta[clave] = leer_metadatos(Path(clave))
        m.update(_cache_meta[clave])
        estado = compat.get(clave)
        m["compatible"] = estado.get("ok") if estado else None
        m["error_compat"] = estado.get("error", "") if estado else ""
        m["apto"] = not m.get("es_embedding", False)
    return sorted(modelos, key=lambda m: (m["origen"], m["nombre"]))


def _cargar_compat() -> dict[str, dict]:
    try:
        return json.loads(CACHE_COMPAT.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def _guardar_compat(datos: dict[str, dict]) -> None:
    try:
        CACHE_COMPAT.write_text(json.dumps(datos, indent=2), "utf-8")
    except OSError:
        pass


def por_id(id_modelo: str) -> dict[str, Any] | None:
    return next((m for m in catalogo() if m["id"] == id_modelo), None)


def comprobar(id_modelo: str, binario_cli: str) -> dict[str, Any]:
    """Intenta cargar el modelo en CPU y guarda si funciona. Cachea el resultado."""
    modelo = por_id(id_modelo)
    if not modelo:
        return {"ok": False, "error": "Modelo no encontrado."}
    if modelo.get("es_embedding"):
        return {"ok": False, "error": "Es un modelo de embeddings: no genera texto."}

    try:
        r = subprocess.run(
            [binario_cli, "-m", modelo["ruta"], "-p", "hola", "-n", "1",
             "-c", "256", "-ngl", "0", "--device", "none", "-st"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=300, creationflags=SIN_VENTANA)
        salida = (r.stdout or "") + (r.stderr or "")
        fallo = re.search(r"error loading model[^\n]*", salida)
        resultado = ({"ok": True, "error": ""} if r.returncode == 0 and not fallo
                     else {"ok": False,
                           "error": (fallo.group(0) if fallo
                                     else f"llama-cli terminó con código {r.returncode}")})
    except subprocess.TimeoutExpired:
        resultado = {"ok": False, "error": "La carga tardó más de 5 minutos."}
    except OSError as exc:
        resultado = {"ok": False, "error": str(exc)}

    compat = _cargar_compat()
    compat[modelo["ruta"]] = resultado
    _guardar_compat(compat)
    return resultado


# ---------------------------------------------------------------------------
# Servidor llama-server
# ---------------------------------------------------------------------------
def _puerto_libre(desde: int = 8081) -> int:
    for puerto in range(desde, desde + 40):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", puerto)) != 0:
                return puerto
    return desde


class ServidorTexto:
    """Envuelve un llama-server: lo arranca, lo para y le habla por HTTP."""

    def __init__(self) -> None:
        self.proceso: subprocess.Popen[str] | None = None
        self.puerto = 0
        self.id_modelo = ""
        self.nombre = ""
        self.dispositivo = ""
        self.error = ""
        self.cargando = False
        self.registro: list[str] = []
        self._cerrojo = threading.Lock()

    # -- estado ------------------------------------------------------------
    @property
    def vivo(self) -> bool:
        return self.proceso is not None and self.proceso.poll() is None

    def listo(self) -> bool:
        if not self.vivo:
            return False
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.puerto}/health", timeout=3) as r:
                return r.status == 200
        except (urllib.error.URLError, OSError):
            return False

    def instantanea(self) -> dict[str, Any]:
        return {
            "cargado": self.vivo and self.listo(),
            "cargando": self.cargando,
            "id_modelo": self.id_modelo,
            "nombre": self.nombre,
            "dispositivo": self.dispositivo,
            "puerto": self.puerto,
            "error": self.error,
            "registro": self.registro[-40:],
        }

    # -- ciclo de vida -----------------------------------------------------
    def parar(self) -> None:
        with self._cerrojo:
            if self.proceso and self.proceso.poll() is None:
                self.proceso.kill()
                try:
                    self.proceso.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
            self.proceso = None
            self.id_modelo = self.nombre = self.dispositivo = ""
            self.cargando = False

    def arrancar(self, binario_server: str, modelo: dict[str, Any],
                 dispositivo: str = "", capas: int = 99, contexto: int = 8192,
                 espera: int = 600) -> bool:
        """`dispositivo` vacío = CPU. Devuelve True cuando el servidor responde."""
        with self._cerrojo:
            if self.vivo and self.id_modelo == modelo["id"] \
                    and self.dispositivo == (dispositivo or "CPU"):
                return self.listo()

        self.parar()
        self.error = ""
        self.registro = []
        self.cargando = True
        self.puerto = _puerto_libre()
        self.id_modelo = modelo["id"]
        self.nombre = modelo["nombre"]
        self.dispositivo = dispositivo or "CPU"

        # El contexto no puede pasar del que declara el modelo
        tope = int(modelo.get("contexto") or 0)
        if tope:
            contexto = min(contexto, tope)

        cmd = [binario_server, "-m", modelo["ruta"],
               "--host", "127.0.0.1", "--port", str(self.puerto),
               "-c", str(contexto), "--jinja"]
        cmd += (["-ngl", str(capas), "--device", dispositivo]
                if dispositivo else ["-ngl", "0", "--device", "none"])

        try:
            self.proceso = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding="utf-8", errors="replace", bufsize=1,
                creationflags=SIN_VENTANA)
        except OSError as exc:
            self.error = f"No se pudo lanzar llama-server: {exc}"
            self.cargando = False
            return False

        threading.Thread(target=self._leer_registro, daemon=True).start()

        inicio = time.time()
        while time.time() - inicio < espera:
            if not self.vivo:
                self.error = self._motivo_del_fallo()
                self.cargando = False
                return False
            if self.listo():
                self.cargando = False
                return True
            time.sleep(0.5)

        self.error = f"El modelo no terminó de cargar en {espera} s."
        self.parar()
        return False

    def _leer_registro(self) -> None:
        proceso = self.proceso
        if not proceso or not proceso.stdout:
            return
        for linea in proceso.stdout:
            linea = linea.rstrip()
            if linea:
                self.registro.append(linea)
                if len(self.registro) > 400:
                    del self.registro[:200]

    def _motivo_del_fallo(self) -> str:
        for linea in reversed(self.registro):
            if "error loading model" in linea or "unknown model architecture" in linea:
                return re.sub(r"^\S+\s+E\s+", "", linea).strip()
        return "llama-server se cerró al cargar el modelo. Mira el registro."

    # -- generación --------------------------------------------------------
    def chat(self, mensajes: list[dict[str, str]], temperatura: float = 0.8,
             top_p: float = 0.95, max_tokens: int = 2048,
             cancelado=None) -> Iterator[str]:
        """Envía la conversación y va devolviendo el texto según llega."""
        if not self.vivo:
            raise RuntimeError("No hay ningún modelo de texto cargado.")

        cuerpo = json.dumps({
            "messages": mensajes, "temperature": temperatura, "top_p": top_p,
            "max_tokens": max_tokens, "stream": True,
        }).encode("utf-8")
        peticion = urllib.request.Request(
            f"http://127.0.0.1:{self.puerto}/v1/chat/completions", cuerpo,
            {"Content-Type": "application/json"})

        with urllib.request.urlopen(peticion, timeout=1800) as respuesta:
            for linea in respuesta:
                if cancelado and cancelado():
                    return
                linea = linea.decode("utf-8", "replace").strip()
                if not linea.startswith("data: "):
                    continue
                if linea == "data: [DONE]":
                    return
                try:
                    trozo = json.loads(linea[6:])
                except ValueError:
                    continue
                delta = (trozo.get("choices") or [{}])[0].get("delta", {})
                if delta.get("content"):
                    yield delta["content"]


SERVIDOR = ServidorTexto()


# ---------------------------------------------------------------------------
# Guiones: del texto del modelo a segmentos con personaje
# ---------------------------------------------------------------------------
LINEA_GUION = re.compile(r"^\s*[\[\(]\s*([^\]\)]{1,40}?)\s*[\]\)]\s*[:\-–]?\s*(.*)$")
LINEA_DOS_PUNTOS = re.compile(r"^\s*([A-ZÁÉÍÓÚÜÑ][\wÁÉÍÓÚÜÑáéíóúüñ .'-]{0,30}?)\s*:\s+(.+)$")


def limpiar_razonamiento(texto: str) -> str:
    """Quita los bloques <think> que emiten algunos modelos."""
    texto = re.sub(r"<think>.*?</think>", "", texto, flags=re.S | re.I)
    return re.sub(r"</?think>", "", texto, flags=re.I).strip()


def analizar_guion(texto: str) -> list[dict[str, str]]:
    """Convierte '[PERSONAJE] frase' (o 'PERSONAJE: frase') en segmentos."""
    segmentos: list[dict[str, str]] = []
    for linea in limpiar_razonamiento(texto).splitlines():
        linea = linea.strip()
        if not linea or linea.startswith(("#", "```", "---")):
            continue

        m = LINEA_GUION.match(linea) or LINEA_DOS_PUNTOS.match(linea)
        if m and m.group(2).strip():
            personaje = re.sub(r"\s+", " ", m.group(1)).strip().upper()
            segmentos.append({"personaje": personaje, "texto": m.group(2).strip()})
        elif segmentos:
            # Continuación de la intervención anterior
            segmentos[-1]["texto"] += " " + linea
        else:
            segmentos.append({"personaje": "NARRADOR", "texto": linea})
    return segmentos


def personajes_de(segmentos: list[dict[str, Any]]) -> list[str]:
    vistos: list[str] = []
    for s in segmentos:
        p = s.get("personaje", "NARRADOR")
        if p not in vistos:
            vistos.append(p)
    return vistos


SISTEMA_GUION = (
    "Eres un guionista. Escribes historias, cuentos y diálogos pensados para ser "
    "locutados en voz alta por varios actores.\n\n"
    "Tu respuesta es SIEMPRE un guion con este formato exacto, una intervención "
    "por línea, sin nada más:\n\n"
    "[NARRADOR] La tarde caía sobre el puerto y las gaviotas callaron de golpe.\n"
    "[ELENA] Abuela, ¿tú viste alguna vez un barco así de grande?\n"
    "[ABUELA] Uno igual no, pequeña. Pero vi cosas que no creerías.\n"
    "[NARRADOR] La anciana señaló el horizonte con la mano temblorosa.\n\n"
    "Reglas estrictas:\n"
    "- Empieza directamente por la primera línea del guion.\n"
    "- Cada línea empieza por [NOMBRE] en mayúsculas y entre corchetes.\n"
    "- Usa [NARRADOR] para la narración y la descripción.\n"
    "- Una sola línea por intervención, sin saltos de línea dentro de ella.\n"
    "- Nada de acotaciones entre paréntesis, ni comillas, ni asteriscos, ni emojis.\n"
    "- Nada de títulos, introducciones, resúmenes ni comentarios finales.\n"
    "- Escribe los números en palabras (dos mil, no 2000).\n"
    "- Mantén los mismos nombres de personaje durante todo el guion.\n"
)


def peticion_guion(idea: str, n_personajes: int = 0, palabras: int = 0,
                   idioma: str = "español") -> list[dict[str, str]]:
    partes = [f"Escribe en {idioma} lo siguiente:", idea.strip()]
    if n_personajes:
        partes.append(f"Deben intervenir exactamente {n_personajes} voces distintas "
                      f"(contando al NARRADOR si lo usas).")
    if palabras:
        partes.append(f"Extensión aproximada: {palabras} palabras.")
    return [{"role": "system", "content": SISTEMA_GUION},
            {"role": "user", "content": "\n\n".join(partes)}]
