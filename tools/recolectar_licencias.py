"""Copia al paquete compilado todas las licencias que hay que repartir con él.

Lo llama build.ps1 después de PyInstaller. Junta dos cosas en
`dist/MyWhisper/licencias/`:

  1. Los textos fijos de `licencias-terceros/` (FFmpeg, x264, x265, el modelo
     y los paquetes que no traen su licencia dentro del wheel).
  2. Los archivos de licencia que cada paquete de Python instalado sí trae,
     leídos de su metadata. Así una actualización de versión no deja un texto
     viejo o faltante.

Uso: python tools/recolectar_licencias.py <carpeta-del-paquete>
"""

from __future__ import annotations

import importlib.metadata as md
import shutil
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]

# Todo lo que PyInstaller mete en el paquete, directo o arrastrado.
PAQUETES = (
    "faster-whisper", "ctranslate2", "av", "onnxruntime", "tokenizers",
    "huggingface-hub", "hf-xet", "numpy", "tqdm", "tkinterdnd2", "sv-ttk",
    "protobuf", "PyYAML", "certifi", "filelock", "fsspec", "httpx", "httpcore",
    "anyio", "idna", "h11", "flatbuffers", "typing_extensions", "packaging",
    "click", "colorama",
)
MARCAS = ("LICENSE", "LICENCE", "COPYING", "NOTICE", "THIRDPARTY")


def main(destino_paquete: Path) -> int:
    destino = destino_paquete / "licencias"
    destino.mkdir(parents=True, exist_ok=True)

    fijos = sorted((RAIZ / "licencias-terceros").glob("*.txt"))
    for texto in fijos:
        shutil.copy2(texto, destino / texto.name)

    copiados, sin_licencia = 0, []
    for nombre in PAQUETES:
        try:
            dist = md.distribution(nombre)
        except md.PackageNotFoundError:
            continue
        archivos = [
            f for f in (dist.files or [])
            if any(m in f.name.upper().replace("-", "") for m in MARCAS)
        ]
        if not archivos:
            sin_licencia.append(nombre)
            continue
        carpeta = destino / f"{dist.metadata['Name']}-{dist.version}"
        carpeta.mkdir(exist_ok=True)
        for f in archivos:
            origen = Path(dist.locate_file(f))
            if origen.is_file():
                # Aplanado con la ruta interna en el nombre: numpy trae varios
                # LICENSE.txt de subcomponentes distintos.
                plano = "__".join(Path(str(f)).parts[-3:])
                shutil.copy2(origen, carpeta / plano)
                copiados += 1

    print(f"Licencias: {len(fijos)} textos fijos + {copiados} de paquetes -> {destino}")
    if sin_licencia:
        print(f"  Sin archivo en el wheel (cubiertos por licencias-terceros): {', '.join(sin_licencia)}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(Path(sys.argv[1])))
