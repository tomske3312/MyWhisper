"""Soporte opcional de GPU NVIDIA.

CTranslate2 (el motor detrás de faster-whisper) trae soporte CUDA compilado,
pero NO trae las librerías de NVIDIA: las carga dinámicamente. Esta app se
distribuye sólo con la parte CPU, y quien tenga una placa NVIDIA puede pedir el
"pack CUDA" desde la interfaz.

Reglas de seguridad que este módulo respeta, a propósito:

  1. Nada de permisos de administrador. Nunca.
  2. No se toca el registro, ni el PATH del sistema, ni variables de usuario.
     El PATH se modifica sólo dentro de este proceso, y muere con él.
  3. No se instalan ni actualizan drivers. El driver es cosa del usuario; acá
     sólo se detecta si ya existe.
  4. Todo lo descargado cae en UNA carpeta dentro de la app (`cuda/`).
     Borrar esa carpeta desinstala el pack por completo.
  5. Versiones fijadas en el código y hash SHA-256 verificado antes de
     descomprimir. Si PyPI devolviera un archivo distinto al esperado, se
     aborta en vez de ejecutarlo.
  6. Del ZIP se extrae sólo una lista blanca de nombres de archivo, aplanada.
     Nunca se respetan las rutas internas del ZIP (protección zip-slip).

La detección de GPU usa `nvidia-smi`, que es de solo lectura y viene con el
driver. Si no está, asumimos que no hay placa NVIDIA utilizable y listo.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

from . import paths

log = logging.getLogger(__name__)

PYPI_JSON = "https://pypi.org/pypi/{package}/{version}/json"
_TIMEOUT = 60


class CancelledError(Exception):
    """El usuario apretó Cancelar durante la descarga."""


@dataclass(frozen=True)
class Wheel:
    package: str
    version: str
    sha256: str
    download_mb: int
    keep: tuple[str, ...]


# Versiones fijadas y verificadas contra una RTX 4060 (driver 610.74) con
# CTranslate2 4.8.1. No actualizar a ciegas: cuDNN cambia de major y
# CTranslate2 queda enganchado a uno concreto.
#
# Sobre lo que NO se incluye:
#   - cudnn_adv64_9.dll (256 MB): son los kernels de RNN/LSTM. cuDNN 9 carga
#     sus sublibrerías bajo demanda y CTranslate2 implementa su propia
#     atención, así que nunca se pide. Podarlo no depende de la GPU.
#   - nvblas64_12.dll: shim BLAS para Fortran, no lo usa nadie acá.
#   - nvrtc64_120_0.alt.dll: duplicado para drivers viejos; ya va el normal.
#
# Sobre lo que SÍ se incluye aunque tiente podarlo:
#   - cudnn_engines_precompiled (460 MB) trae kernels por arquitectura de GPU.
#     Acá funciona sin él, pero eso sólo prueba que sobra para sm_89. En otra
#     placa haría falta, y no hay forma de verificarlo desde esta máquina.
#   - nvrtc (93 MB) es el compilador en runtime, el plan B de cuDNN cuando no
#     encuentra un kernel precompilado para la placa. Es el seguro contra
#     tarjetas que no probamos.
CUDA_PACK: tuple[Wheel, ...] = (
    Wheel(
        package="nvidia-cublas-cu12",
        version="12.9.2.10",
        sha256="623f43027d40d44ceadf0043f002bd25cf353e8f13ce90b9a87057019f560661",
        download_mb=528,
        keep=("cublas64_12.dll", "cublasLt64_12.dll"),
    ),
    Wheel(
        package="nvidia-cudnn-cu12",
        version="9.23.0.39",
        sha256="357e5d59a1b79d27eef754aa79b3d9e7adf11baf86dc928dc114df0033c2c912",
        download_mb=658,
        keep=(
            "cudnn64_9.dll",
            "cudnn_graph64_9.dll",
            "cudnn_ops64_9.dll",
            "cudnn_cnn64_9.dll",
            "cudnn_heuristic64_9.dll",
            "cudnn_engines_precompiled64_9.dll",
            "cudnn_engines_runtime_compiled64_9.dll",
            "cudnn_ext64_9.dll",
            "cudnn_engines_tensor_ir64_9.dll",
        ),
    ),
    Wheel(
        package="nvidia-cuda-runtime-cu12",
        version="12.9.79",
        sha256="8e018af8fa02363876860388bd10ccb89eb9ab8fb0aa749aaf58430a9f7c4891",
        download_mb=4,
        keep=("cudart64_12.dll",),
    ),
    Wheel(
        package="nvidia-cuda-nvrtc-cu12",
        version="12.9.86",
        sha256="72972ebdcf504d69462d3bcd67e7b81edd25d0fb85a2c46d3ea3517666636349",
        download_mb=73,
        keep=("nvrtc64_120_0.dll", "nvrtc-builtins64_129.dll"),
    ),
)

DOWNLOAD_MB = sum(w.download_mb for w in CUDA_PACK)
INSTALLED_MB = 1580

# Si estos tres no están, el pack no sirve; se usan como marca de "instalado".
_SENTINELS = ("cublas64_12.dll", "cudnn64_9.dll", "cudart64_12.dll")


# --------------------------------------------------------------------------
# Detección
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class GpuInfo:
    name: str
    vram_mb: int
    driver: str

    @property
    def vram_gb(self) -> float:
        return self.vram_mb / 1024


def detect_nvidia() -> GpuInfo | None:
    """Consulta `nvidia-smi`. Solo lectura, sin efectos sobre el sistema."""
    try:
        # CREATE_NO_WINDOW evita el parpadeo de una consola negra al consultar.
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        out = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,driver_version",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=creationflags,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None

    if out.returncode != 0 or not out.stdout.strip():
        return None

    first = out.stdout.strip().splitlines()[0]
    parts = [p.strip() for p in first.split(",")]
    if len(parts) < 3:
        return None
    try:
        vram = int(float(parts[1]))
    except ValueError:
        vram = 0
    return GpuInfo(name=parts[0], vram_mb=vram, driver=parts[2])


def pack_installed() -> bool:
    d = paths.cuda_dir()
    return all((d / name).exists() for name in _SENTINELS)


def pack_size_mb() -> int:
    d = paths.cuda_dir()
    if not d.exists():
        return 0
    return int(sum(f.stat().st_size for f in d.glob("*.dll")) / 1048576)


def remove_pack() -> None:
    """Desinstalar es borrar una carpeta. Esa es toda la gracia."""
    shutil.rmtree(paths.cuda_dir(), ignore_errors=True)


# --------------------------------------------------------------------------
# Activación
# --------------------------------------------------------------------------

_activated = False


def activate() -> bool:
    """Deja las DLL del pack visibles para el cargador de Windows.

    TIENE que llamarse ANTES de importar ctranslate2 / faster_whisper: una vez
    que la DLL del motor se cargó, agregar rutas no cambia nada.

    Ambas mutaciones son locales al proceso y desaparecen al cerrar la app.
    """
    global _activated
    if _activated:
        return True
    if not pack_installed():
        return False

    d = str(paths.cuda_dir())
    os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
    try:
        os.add_dll_directory(d)
    except (OSError, AttributeError):
        log.warning("add_dll_directory falló para %s", d, exc_info=True)
    _activated = True
    log.info("Pack CUDA activado desde %s", d)
    return True


def cuda_is_working() -> tuple[bool, str]:
    """Prueba real: pedirle a CTranslate2 que enumere dispositivos CUDA.

    Se hace después de `activate()`. Si el pack está incompleto o el driver es
    viejo, acá salta — y no en mitad de una transcripción de una hora.
    """
    try:
        import ctranslate2

        count = ctranslate2.get_cuda_device_count()
    except Exception as exc:
        return False, f"No se pudo consultar CUDA: {exc}"
    if count < 1:
        return False, "CUDA no encontró ninguna placa utilizable."
    return True, f"CUDA operativo ({count} placa/s)."


# --------------------------------------------------------------------------
# Descarga del pack
# --------------------------------------------------------------------------


def _resolve_wheel_url(wheel: Wheel) -> tuple[str, int]:
    """Pide a PyPI la URL del wheel de Windows y confirma que su hash publicado
    coincide con el que tenemos fijado acá. Si no coincide, no se descarga."""
    url = PYPI_JSON.format(package=wheel.package, version=wheel.version)
    with urllib.request.urlopen(url, timeout=_TIMEOUT) as resp:
        data = json.load(resp)

    for entry in data.get("urls", []):
        if not entry.get("filename", "").endswith("win_amd64.whl"):
            continue
        published = entry.get("digests", {}).get("sha256", "")
        if published != wheel.sha256:
            raise RuntimeError(
                f"{wheel.package} {wheel.version}: el hash publicado por PyPI no "
                f"coincide con el esperado. Descarga cancelada por seguridad."
            )
        return entry["url"], int(entry.get("size", 0))

    raise RuntimeError(f"{wheel.package} {wheel.version}: no hay wheel para Windows x64.")


def _download_verified(
    url: str,
    expected_sha256: str,
    dest: Path,
    on_chunk,
    cancel: threading.Event,
) -> None:
    digest = hashlib.sha256()
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=_TIMEOUT) as resp, tmp.open("wb") as fh:
            while True:
                if cancel.is_set():
                    raise CancelledError
                chunk = resp.read(1024 * 512)
                if not chunk:
                    break
                digest.update(chunk)
                fh.write(chunk)
                on_chunk(len(chunk))

        actual = digest.hexdigest()
        if actual != expected_sha256:
            raise RuntimeError(
                "El archivo descargado no coincide con su firma SHA-256. "
                "Se descartó sin abrirlo."
            )
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)


def _extract_allowlisted(archive: Path, keep: tuple[str, ...], dest_dir: Path) -> list[str]:
    """Saca del wheel sólo los nombres de la lista blanca, aplanados.

    Nunca se usa la ruta interna del ZIP para escribir: se toma el basename y
    se compara contra `keep`. Un ZIP malicioso con `..\\..\\system32\\algo.dll`
    no tiene por dónde salir de `dest_dir`.
    """
    extracted: list[str] = []
    with zipfile.ZipFile(archive) as zf:
        for member in zf.infolist():
            if member.is_dir():
                continue
            name = Path(member.filename).name
            if name not in keep:
                continue
            target = dest_dir / name
            with zf.open(member) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out, length=1024 * 1024)
            extracted.append(name)
    return extracted


def download_pack(on_progress, cancel: threading.Event) -> None:
    """Descarga, verifica e instala el pack CUDA.

    `on_progress(fraccion_0_a_1, texto)` se llama seguido; corre en el hilo que
    llame a esta función, así que la UI tiene que hacer el marshalling.
    """
    dest_dir = paths.cuda_dir()
    staging = dest_dir.with_name(dest_dir.name + ".descargando")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)

    total_mb = sum(w.download_mb for w in CUDA_PACK)
    total_bytes = total_mb * 1048576
    done_bytes = 0

    try:
        for index, wheel in enumerate(CUDA_PACK, start=1):
            if cancel.is_set():
                raise CancelledError

            etiqueta = f"({index}/{len(CUDA_PACK)}) {wheel.package}"
            on_progress(done_bytes / total_bytes, f"Verificando {etiqueta}…")
            url, _size = _resolve_wheel_url(wheel)

            archive = staging / f"{wheel.package}.whl"
            base = done_bytes

            def on_chunk(n: int, _base=base, _label=etiqueta) -> None:
                nonlocal done_bytes
                done_bytes += n
                on_progress(
                    min(done_bytes / total_bytes, 1.0),
                    f"Descargando {_label} — {done_bytes / 1048576:.0f} de "
                    f"{total_mb} MB",
                )

            _download_verified(url, wheel.sha256, archive, on_chunk, cancel)

            on_progress(min(done_bytes / total_bytes, 1.0), f"Instalando {etiqueta}…")
            got = _extract_allowlisted(archive, wheel.keep, staging)
            faltantes = set(wheel.keep) - set(got)
            if faltantes:
                raise RuntimeError(
                    f"{wheel.package}: faltaron archivos esperados ({', '.join(sorted(faltantes))})."
                )
            archive.unlink(missing_ok=True)

        # Recién ahora se reemplaza la carpeta buena. Si algo falló antes, el
        # pack anterior (o la ausencia de pack) queda intacto.
        on_progress(1.0, "Finalizando…")
        shutil.rmtree(dest_dir, ignore_errors=True)
        staging.replace(dest_dir)
        log.info("Pack CUDA instalado en %s", dest_dir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
