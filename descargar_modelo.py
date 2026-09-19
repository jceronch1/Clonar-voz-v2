# -*- coding: utf-8 -*-
"""
Descarga los modelos GGUF de Qwen3-TTS desde Hugging Face.

Se usa de dos maneras:

  * Desde la terminal, durante la instalación:
        python descargar_modelo.py
        python descargar_modelo.py --modelo Q8_0 --mmproj bf16

  * Importado por `app.py`, para descargar desde la propia web.

La descarga es reanudable (cabecera HTTP Range): si se corta a mitad, al
repetirla continúa donde iba en lugar de empezar de cero. Al terminar verifica
el tamaño y el SHA-256 de cada archivo.
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

REPO = "ggml-org/Qwen3-TTS-12Hz-1.7B-Base-GGUF"
BASE_URL = f"https://huggingface.co/{REPO}/resolve/main"
DIR_MODELO = Path(__file__).resolve().parent / "modelo"

# Hacen falta DOS archivos: el modelo principal y su proyector multimodal
# (mmproj), que es el vocoder que convierte los tokens en forma de onda.
CATALOGO: dict[str, dict[str, dict]] = {
    "modelo": {
        "Q4_K_M": {
            "archivo": "Qwen3-TTS-12Hz-1.7B-Base-Q4_K_M.gguf",
            "bytes": 1035965280,
            "sha256": "8d18c94acb2addd042f97da63c98be144eafa76d0d9495177eab65130cf85129",
            "etiqueta": "Q4_K_M · 1,04 GB · recomendado",
        },
        "Q8_0": {
            "archivo": "Qwen3-TTS-12Hz-1.7B-Base-Q8_0.gguf",
            "bytes": 1847874400,
            "sha256": "ac7931aeb2e7aad1a6ed6602d353a5679c9d096b18ce8204ac730a8408d572e1",
            "etiqueta": "Q8_0 · 1,85 GB · más calidad",
        },
        "bf16": {
            "archivo": "Qwen3-TTS-12Hz-1.7B-Base-bf16.gguf",
            "bytes": 3472593760,
            "sha256": "0322c634ad5d3282524bc45bff030ab3f6f2a32ba14d5e7dace7eed75ecede46",
            "etiqueta": "BF16 · 3,47 GB · sin cuantizar",
        },
    },
    "mmproj": {
        "Q8_0": {
            "archivo": "mmproj-Qwen3-TTS-12Hz-1.7B-Base-Q8_0.gguf",
            "bytes": 446422912,
            "sha256": "6fd65188839bcd6ecc91b277ad471e22a0edfada4699a0fe82f1165c18cfcce2",
            "etiqueta": "Q8_0 · 446 MB · recomendado",
        },
        "bf16": {
            "archivo": "mmproj-Qwen3-TTS-12Hz-1.7B-Base-bf16.gguf",
            "bytes": 669081472,
            "sha256": "b9503e95e44705739cf82c15ce909b040b96a31ad79d02cf4dc5e1484399265d",
            "etiqueta": "BF16 · 669 MB · sin cuantizar",
        },
    },
}

POR_DEFECTO = {"modelo": "Q4_K_M", "mmproj": "Q8_0"}

# progreso(archivo, descargado, total, bytes_por_segundo)
Progreso = Callable[[str, int, int, float], None]


def legible(n: float) -> str:
    for unidad in ("B", "KB", "MB", "GB"):
        if n < 1024 or unidad == "GB":
            return f"{n:.1f} {unidad}" if unidad != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} GB"


def sha256_de(ruta: Path, progreso: Progreso | None = None) -> str:
    h = hashlib.sha256()
    total = ruta.stat().st_size
    leido = 0
    inicio = time.time()
    with open(ruta, "rb") as f:
        for bloque in iter(lambda: f.read(1 << 22), b""):
            h.update(bloque)
            leido += len(bloque)
            if progreso:
                transcurrido = max(time.time() - inicio, 1e-6)
                progreso(f"Verificando {ruta.name}", leido, total, leido / transcurrido)
    return h.hexdigest()


def ya_esta(destino: Path, info: dict) -> bool:
    """Un archivo vale si existe y su tamaño coincide exactamente."""
    return destino.is_file() and destino.stat().st_size == info["bytes"]


def descargar_archivo(info: dict, carpeta: Path, progreso: Progreso | None = None,
                      verificar: bool = True, cancelado: Callable[[], bool] | None = None) -> Path:
    """Descarga un archivo del catálogo, reanudando si hay una descarga a medias."""
    carpeta.mkdir(parents=True, exist_ok=True)
    destino = carpeta / info["archivo"]
    parcial = carpeta / (info["archivo"] + ".parte")
    total = info["bytes"]

    if ya_esta(destino, info):
        if progreso:
            progreso(info["archivo"], total, total, 0.0)
        return destino

    # Espacio libre: pedimos el tamaño del archivo más un 10 % de margen
    libre = shutil.disk_usage(carpeta).free
    faltan = total - (parcial.stat().st_size if parcial.exists() else 0)
    if libre < faltan * 1.1:
        raise RuntimeError(
            f"Espacio insuficiente en disco: hacen falta ~{legible(faltan * 1.1)} "
            f"y solo hay {legible(libre)} libres."
        )

    desde = parcial.stat().st_size if parcial.exists() else 0
    if desde > total:                 # descarga previa corrupta
        parcial.unlink()
        desde = 0

    peticion = urllib.request.Request(
        f"{BASE_URL}/{info['archivo']}?download=true",
        headers={"User-Agent": "clonar-voz/1.0"},
    )
    if desde:
        peticion.add_header("Range", f"bytes={desde}-")

    try:
        respuesta = urllib.request.urlopen(peticion, timeout=60)
    except urllib.error.HTTPError as e:
        if e.code == 416:             # rango inválido: la parte previa no sirve
            desde = 0
            parcial.unlink(missing_ok=True)
            respuesta = urllib.request.urlopen(
                urllib.request.Request(f"{BASE_URL}/{info['archivo']}?download=true",
                                       headers={"User-Agent": "clonar-voz/1.0"}),
                timeout=60)
        else:
            raise RuntimeError(f"Hugging Face devolvió HTTP {e.code} al pedir "
                               f"{info['archivo']}.") from e

    # Si pedimos un rango y el servidor lo ignora (200 en vez de 206), empezamos de cero
    if desde and respuesta.status != 206:
        desde = 0
        parcial.unlink(missing_ok=True)

    modo = "ab" if desde else "wb"
    descargado = desde
    inicio = time.time()
    ultimo_aviso = 0.0

    with respuesta, open(parcial, modo) as f:
        while True:
            if cancelado and cancelado():
                raise RuntimeError("Descarga cancelada.")
            trozo = respuesta.read(1 << 20)
            if not trozo:
                break
            f.write(trozo)
            descargado += len(trozo)
            ahora = time.time()
            if progreso and (ahora - ultimo_aviso > 0.25 or descargado >= total):
                ultimo_aviso = ahora
                velocidad = (descargado - desde) / max(ahora - inicio, 1e-6)
                progreso(info["archivo"], descargado, total, velocidad)

    if parcial.stat().st_size != total:
        raise RuntimeError(
            f"{info['archivo']}: se esperaban {total} bytes y llegaron "
            f"{parcial.stat().st_size}. Vuelve a lanzar la descarga para reanudarla."
        )

    if verificar:
        obtenido = sha256_de(parcial, progreso)
        if obtenido != info["sha256"]:
            parcial.unlink(missing_ok=True)
            raise RuntimeError(f"{info['archivo']}: el SHA-256 no coincide. "
                               "El archivo llegó corrupto; repite la descarga.")

    parcial.replace(destino)
    return destino


def descargar_todo(clave_modelo: str = "Q4_K_M", clave_mmproj: str = "Q8_0",
                   carpeta: Path | None = None, progreso: Progreso | None = None,
                   verificar: bool = True,
                   cancelado: Callable[[], bool] | None = None) -> list[Path]:
    """Descarga el par modelo + mmproj y devuelve las rutas resultantes."""
    carpeta = carpeta or DIR_MODELO
    if clave_modelo not in CATALOGO["modelo"]:
        raise ValueError(f"Cuantización de modelo desconocida: {clave_modelo}")
    if clave_mmproj not in CATALOGO["mmproj"]:
        raise ValueError(f"Cuantización de mmproj desconocida: {clave_mmproj}")

    return [
        descargar_archivo(CATALOGO["modelo"][clave_modelo], carpeta, progreso,
                          verificar, cancelado),
        descargar_archivo(CATALOGO["mmproj"][clave_mmproj], carpeta, progreso,
                          verificar, cancelado),
    ]


def falta_algo(carpeta: Path | None = None) -> bool:
    """True si en la carpeta no hay un par modelo + mmproj utilizable."""
    carpeta = carpeta or DIR_MODELO
    if not carpeta.is_dir():
        return True
    ggufs = list(carpeta.glob("*.gguf"))
    hay_mmproj = any(g.name.lower().startswith("mmproj") for g in ggufs)
    hay_modelo = any(not g.name.lower().startswith("mmproj") for g in ggufs)
    return not (hay_mmproj and hay_modelo)


# ---------------------------------------------------------------------------
# Interfaz de terminal
# ---------------------------------------------------------------------------
def barra_terminal(nombre: str, hecho: int, total: int, velocidad: float) -> None:
    porcentaje = (hecho / total * 100) if total else 0
    lleno = int(porcentaje / 100 * 32)
    restante = (total - hecho) / velocidad if velocidad > 0 else 0
    sys.stdout.write(
        f"\r  {nombre[:38]:<38} [{'█' * lleno}{'·' * (32 - lleno)}] "
        f"{porcentaje:5.1f}%  {legible(velocidad)}/s  faltan {int(restante // 60)}m{int(restante % 60):02d}s   "
    )
    sys.stdout.flush()
    if hecho >= total:
        sys.stdout.write("\n")


def main() -> int:
    p = argparse.ArgumentParser(
        description="Descarga los modelos GGUF de Qwen3-TTS desde Hugging Face.")
    p.add_argument("--modelo", default=POR_DEFECTO["modelo"],
                   choices=list(CATALOGO["modelo"]), help="cuantización del modelo principal")
    p.add_argument("--mmproj", default=POR_DEFECTO["mmproj"],
                   choices=list(CATALOGO["mmproj"]), help="cuantización del proyector/vocoder")
    p.add_argument("--dir", type=Path, default=DIR_MODELO, help="carpeta de destino")
    p.add_argument("--sin-verificar", action="store_true",
                   help="omite la comprobación SHA-256 (más rápido, menos seguro)")
    args = p.parse_args()

    info_m = CATALOGO["modelo"][args.modelo]
    info_p = CATALOGO["mmproj"][args.mmproj]
    total = info_m["bytes"] + info_p["bytes"]

    print()
    print("  Descarga de modelos Qwen3-TTS")
    print(f"  Origen : https://huggingface.co/{REPO}")
    print(f"  Destino: {args.dir}")
    print(f"  Total  : {legible(total)}  ({info_m['etiqueta']} + {info_p['etiqueta']})")
    print()

    try:
        rutas = descargar_todo(args.modelo, args.mmproj, args.dir,
                               barra_terminal, not args.sin_verificar)
    except KeyboardInterrupt:
        print("\n\n  Interrumpido. Vuelve a ejecutarlo para reanudar la descarga.")
        return 1
    except Exception as exc:  # noqa: BLE001 — mensaje directo para el usuario
        print(f"\n\n  [X] {exc}")
        return 1

    print()
    for r in rutas:
        print(f"  [OK] {r.name}  ({legible(r.stat().st_size)})")
    print("\n  Modelos listos. Ya puedes arrancar la aplicación.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
