# -*- coding: utf-8 -*-
"""
Dictado por micrófono: audio -> texto, con la puntuación española cuidada.

Usa faster-whisper (CTranslate2), que corre en CUDA o en CPU. La transcripción
pasa por tres capas pensadas para que los signos lleguen bien al modelo de voz,
porque Qwen3-TTS usa `¿` y `¡` como pista de entonación:

1. `initial_prompt`  — sesga a Whisper hacia la puntuación completa en español.
2. Comandos hablados — «abre interrogación», «coma», «punto y aparte»…
3. Normalizador      — si una frase acaba en `?` o `!` y le falta la apertura,
                       se la pone. Determinista, sin depender del modelo.
"""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import Any

MODELOS = {
    "tiny": "tiny · 75 MB · el más rápido",
    "base": "base · 145 MB",
    "small": "small · 460 MB · recomendado",
    "medium": "medium · 1,5 GB",
    "large-v3": "large-v3 · 2,9 GB · el más preciso",
    "distil-large-v3": "distil-large-v3 · 1,5 GB · rápido y preciso",
}

# Frases de ejemplo cargadas de signos: empujan a Whisper a escribirlos.
PROMPT_ES = ("Transcripción en español con puntuación completa. "
             "¿Cómo estás? ¡Qué alegría verte! ¿Vendrás mañana? "
             "¡Cuidado con el escalón! ¿De verdad? ¡Claro que sí! "
             "¿Qué hora es? ¡Vaya sorpresa!")

# Comandos de dictado. El orden importa: los más largos, primero.
COMANDOS: list[tuple[str, str]] = [
    (r"abre?(?:\s+el)?\s+interrogaci[oó]n", "¿"),
    (r"abrir?(?:\s+el)?\s+interrogaci[oó]n", "¿"),
    (r"cierra?(?:\s+el)?\s+interrogaci[oó]n", "?"),
    (r"cerrar?(?:\s+el)?\s+interrogaci[oó]n", "?"),
    (r"abre?(?:\s+la)?\s+exclamaci[oó]n", "¡"),
    (r"abrir?(?:\s+la)?\s+exclamaci[oó]n", "¡"),
    (r"cierra?(?:\s+la)?\s+exclamaci[oó]n", "!"),
    (r"cerrar?(?:\s+la)?\s+exclamaci[oó]n", "!"),
    (r"signo de interrogaci[oó]n", "?"),
    (r"signo de exclamaci[oó]n", "!"),
    (r"abre?(?:\s+el)?\s+par[eé]ntesis", "("),
    (r"cierra?(?:\s+el)?\s+par[eé]ntesis", ")"),
    (r"puntos suspensivos", "…"),
    (r"punto y aparte", ".\n"),
    (r"punto y coma", ";"),
    (r"punto y seguido", "."),
    (r"nueva l[ií]nea", "\n"),
    (r"salto de l[ií]nea", "\n"),
    (r"dos puntos", ":"),
    (r"punto final", "."),
]
_COMANDOS = [(re.compile(rf"(?<!\w){p}(?!\w)", re.I), s) for p, s in COMANDOS]

# "coma" y "punto" sueltos son palabras corrientes ("el punto de encuentro",
# "que coma despacio"), así que solo cuentan como comando si no van detrás de un
# determinante. El lookbehind de Python es de ancho fijo, así que se captura el
# determinante y se decide en la función de reemplazo.
_DETERMINANTES = r"(\b(?:el|la|un|una|los|las|este|esta|ese|esa|mi|tu|su|del|al|que)\s+)?"
_SUELTOS = [(re.compile(_DETERMINANTES + r"\bcoma\b", re.I), ","),
            (re.compile(_DETERMINANTES + r"\bpunto\b", re.I), ".")]


def _sustituir_suelto(signo: str):
    def reemplazo(m: re.Match) -> str:
        return m.group(0) if m.group(1) else signo
    return reemplazo

FIN_ORACION = ".?!…"


def aplicar_comandos(texto: str) -> str:
    """Convierte «abre interrogación», «coma», «punto y aparte»… en signos."""
    for patron, signo in _COMANDOS:
        texto = patron.sub(signo, texto)
    for patron, signo in _SUELTOS:
        texto = patron.sub(_sustituir_suelto(signo), texto)
    # Los signos han quedado rodeados de espacios: recolocarlos
    texto = re.sub(r"\s+([,.;:!?…\)])", r"\1", texto)
    texto = re.sub(r"([¿¡\(])\s+", r"\1", texto)
    texto = re.sub(r"[ \t]{2,}", " ", texto)
    return re.sub(r" *\n *", "\n", texto).strip()


def _partir_oraciones(texto: str) -> list[str]:
    """Corta el texto tras cada signo de cierre, conservándolo."""
    oraciones, actual = [], ""
    for caracter in texto:
        actual += caracter
        if caracter in FIN_ORACION:
            oraciones.append(actual)
            actual = ""
    if actual.strip():
        oraciones.append(actual)
    return oraciones


def normalizar_signos(texto: str) -> str:
    """Pone el `¿` o el `¡` de apertura cuando la frase acaba en `?` o `!`.

    El signo se coloca al principio de la oración. No siempre es donde lo
    pondría un humano ("Hola, ¿cómo estás?" acaba como "¿Hola, cómo estás?"),
    pero deja el texto bien formado y le da al modelo de voz la entonación
    correcta, que es lo que importa aquí.
    """
    lineas = []
    for linea in texto.split("\n"):          # los saltos son intencionados
        oraciones = []
        for oracion in _partir_oraciones(linea):
            nucleo = oracion.strip()
            if not nucleo:
                continue
            if nucleo.endswith("?") and "¿" not in nucleo:
                nucleo = "¿" + nucleo
            elif nucleo.endswith("!") and "¡" not in nucleo:
                nucleo = "¡" + nucleo
            oraciones.append(nucleo)
        lineas.append(" ".join(oraciones))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lineas)).strip()


def limpiar(texto: str, usar_comandos: bool = True) -> str:
    texto = re.sub(r"\s+", " ", texto or "").strip()
    if usar_comandos:
        texto = aplicar_comandos(texto)
    return normalizar_signos(texto)


# ---------------------------------------------------------------------------
# Motor de transcripción
# ---------------------------------------------------------------------------
class Transcriptor:
    """Envuelve el modelo de faster-whisper: lo carga, lo suelta y transcribe."""

    def __init__(self) -> None:
        self.modelo: Any = None
        self.tam = ""
        self.dispositivo = ""
        self.tipo_computo = ""
        self.error = ""
        self.cargando = False
        self._cerrojo = threading.Lock()

    # -- disponibilidad ----------------------------------------------------
    @staticmethod
    def disponible() -> bool:
        try:
            import faster_whisper  # noqa: F401
            return True
        except ImportError:
            return False

    @staticmethod
    def hay_cuda() -> bool:
        try:
            import ctranslate2
            return ctranslate2.get_cuda_device_count() > 0
        except Exception:  # noqa: BLE001 — cualquier fallo = sin CUDA
            return False

    def instantanea(self) -> dict[str, Any]:
        return {
            "disponible": self.disponible(),
            "cuda": self.hay_cuda(),
            "cargado": self.modelo is not None,
            "cargando": self.cargando,
            "modelo": self.tam,
            "dispositivo": self.dispositivo,
            "tipo_computo": self.tipo_computo,
            "error": self.error,
            "modelos": MODELOS,
        }

    # -- ciclo de vida -----------------------------------------------------
    def parar(self) -> None:
        with self._cerrojo:
            self.modelo = None
            self.tam = self.dispositivo = self.tipo_computo = ""
        try:
            import gc
            import torch
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001 — liberar es best-effort
            pass

    def cargar(self, tam: str = "small", dispositivo: str = "auto") -> bool:
        if not self.disponible():
            self.error = ("Falta faster-whisper. Instálalo con: "
                          "pip install faster-whisper")
            return False

        if dispositivo == "auto":
            dispositivo = "cuda" if self.hay_cuda() else "cpu"
        tipo = "float16" if dispositivo == "cuda" else "int8"

        with self._cerrojo:
            if (self.modelo is not None and self.tam == tam
                    and self.dispositivo == dispositivo):
                return True

        self.parar()
        self.cargando = True
        self.error = ""
        try:
            from faster_whisper import WhisperModel
            modelo = WhisperModel(tam, device=dispositivo, compute_type=tipo)
        except Exception as exc:  # noqa: BLE001 — se enseña tal cual en la web
            self.error = str(exc)
            self.cargando = False
            return False

        with self._cerrojo:
            self.modelo = modelo
            self.tam, self.dispositivo, self.tipo_computo = tam, dispositivo, tipo
        self.cargando = False
        return True

    # -- transcripción -----------------------------------------------------
    def transcribir(self, ruta: Path, idioma: str = "es",
                    usar_comandos: bool = True) -> dict[str, Any]:
        if self.modelo is None:
            raise RuntimeError("No hay ningún modelo de dictado cargado.")

        inicio = time.time()
        segmentos, info = self.modelo.transcribe(
            str(ruta),
            language=None if idioma in ("auto", "") else idioma,
            beam_size=5,
            temperature=0,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 300},
            initial_prompt=PROMPT_ES if idioma in ("es", "auto", "") else None,
            condition_on_previous_text=False,
        )
        bruto = "".join(s.text for s in segmentos).strip()
        return {
            "texto": limpiar(bruto, usar_comandos),
            "bruto": bruto,
            "idioma": info.language,
            "duracion": round(info.duration, 2),
            "segundos": round(time.time() - inicio, 2),
        }


TRANSCRIPTOR = Transcriptor()


# ---------------------------------------------------------------------------
# Repaso opcional de la puntuación con el modelo de texto
# ---------------------------------------------------------------------------
SISTEMA_PUNTUACION = (
    "Corriges la puntuación de transcripciones en español. Devuelves EXACTAMENTE "
    "las mismas palabras, en el mismo orden, sin añadir ni quitar ninguna: solo "
    "ajustas signos, tildes y mayúsculas.\n"
    "Presta especial atención a abrir `¿` y `¡` cuando la frase sea una pregunta "
    "o una exclamación, y a cerrarlas con `?` o `!`.\n"
    "Responde solo con el texto corregido, sin comillas ni explicaciones."
)


def _palabras(texto: str) -> list[str]:
    """Palabras en minúscula y sin tildes: el LLM sí puede corregir acentos."""
    import unicodedata
    plano = unicodedata.normalize("NFD", texto.lower())
    plano = "".join(c for c in plano if unicodedata.category(c) != "Mn")
    return re.findall(r"\w+", plano, re.UNICODE)


def pulir_con_llm(texto: str, servidor: Any, temperatura: float = 0.2) -> str:
    """Pide al LLM que arregle solo los signos. Si se desvía, se descarta.

    El modelo podría reescribir o inventar; se compara palabra por palabra y solo
    se acepta el resultado si dice exactamente lo mismo.
    """
    if not texto.strip() or servidor is None or not getattr(servidor, "vivo", False):
        return texto
    try:
        salida = "".join(servidor.chat(
            [{"role": "system", "content": SISTEMA_PUNTUACION},
             {"role": "user", "content": texto}],
            temperatura=temperatura, top_p=0.9,
            max_tokens=max(64, len(texto) // 2 + 64)))
    except Exception:  # noqa: BLE001 — si el LLM falla, nos quedamos como estábamos
        return texto

    limpio = re.sub(r"<think>.*?</think>", "", salida, flags=re.S | re.I).strip()
    limpio = limpio.strip('"').strip()
    if not limpio or _palabras(limpio) != _palabras(texto):
        return texto                     # cambió las palabras: no nos sirve
    return normalizar_signos(limpio)
