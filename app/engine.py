"""Motor de transcripción: carga del modelo, progreso real y cancelación.

Dos cosas guían el diseño de este módulo:

  1. **La app tiene que poder quedar transcribiendo sin que la máquina se
     arrastre.** Por eso el trabajo va en un hilo aparte, con la cantidad de
     hilos de CPU limitada y la prioridad del proceso bajada. Ver
     `apply_cpu_profile()`.
  2. **El progreso tiene que ser real, no una barra que gira.** faster-whisper
     devuelve un generador perezoso y un `info.duration` inmediato, así que
     `segmento.end / info.duration` da el porcentaje exacto sin costo.

`faster_whisper` se importa DENTRO de las funciones a propósito: importarlo
carga la DLL de CTranslate2, y eso tiene que pasar después de que `gpu.activate()`
haya registrado las rutas del pack CUDA.
"""

from __future__ import annotations

import ctypes
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import paths, settings as settings_mod

log = logging.getLogger(__name__)

# Formatos que PyAV (incluido) sabe abrir. La lista es para el diálogo de
# "Abrir archivo" y para avisar temprano; si llega otra cosa igual se intenta.
AUDIO_EXTENSIONS = (
    ".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".wma",
    ".mp4", ".mkv", ".mov", ".avi", ".webm",
)

_BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
_NORMAL_PRIORITY_CLASS = 0x00000020


class Cancelled(Exception):
    pass


def apply_cpu_profile(profile: str) -> int:
    """Ajusta prioridad del proceso y devuelve cuántos hilos usar.

    Bajar la prioridad a BELOW_NORMAL es lo que hace la diferencia entre "puedo
    seguir usando el PC" y "el PC no responde": Windows le da los ciclos a la
    ventana en primer plano y la transcripción usa lo que sobra. Apenas cuesta
    velocidad cuando la máquina está ociosa, que es el caso normal.

    No se usa PROCESS_MODE_BACKGROUND_BEGIN porque además baja la prioridad de
    I/O y de memoria, y ahí sí la transcripción se vuelve el doble de lenta.
    """
    spec = settings_mod.CPU_PROFILES.get(profile, settings_mod.CPU_PROFILES["equilibrado"])
    cores = os.cpu_count() or 4
    threads = max(2, int(cores * spec["core_fraction"]))

    if os.name == "nt":
        flag = _BELOW_NORMAL_PRIORITY_CLASS if spec["below_normal"] else _NORMAL_PRIORITY_CLASS
        try:
            kernel32 = ctypes.windll.kernel32
            kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), flag)
        except Exception:
            log.warning("No se pudo ajustar la prioridad del proceso", exc_info=True)

    return threads


def restore_priority() -> None:
    if os.name != "nt":
        return
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), _NORMAL_PRIORITY_CLASS)
    except Exception:
        pass


# --------------------------------------------------------------------------
# Modelos
# --------------------------------------------------------------------------


def ensure_model(name: str, on_progress, cancel: threading.Event) -> Path:
    """Devuelve la carpeta del modelo, descargándolo si hace falta.

    `base` y `small` vienen empaquetados, así que esto sólo baja algo si el
    usuario eligió `medium` o `large-v3` a mano.
    """
    local = paths.find_model(name)
    if local is not None:
        return local

    if cancel.is_set():
        raise Cancelled

    spec = settings_mod.MODELS.get(name, {})
    size = spec.get("size_mb", 0)
    on_progress(0.0, f"Descargando el modelo «{name}» (≈{size} MB), una sola vez…")

    from huggingface_hub import snapshot_download

    target = paths.downloaded_models_dir() / name
    target.mkdir(parents=True, exist_ok=True)

    kwargs = dict(
        repo_id=f"Systran/faster-whisper-{name}",
        local_dir=str(target),
        allow_patterns=["*.bin", "*.json", "*.txt"],
    )
    if spec.get("revision"):
        kwargs["revision"] = spec["revision"]
    try:
        kwargs["tqdm_class"] = _make_tqdm(on_progress, cancel, size)
        snapshot_download(**kwargs)
    except TypeError:
        # Alguna versión de huggingface_hub sin `tqdm_class`: se baja igual,
        # sólo que sin porcentaje.
        kwargs.pop("tqdm_class", None)
        snapshot_download(**kwargs)

    found = paths.find_model(name)
    if found is None:
        raise RuntimeError(f"El modelo «{name}» se descargó incompleto.")

    esperado = spec.get("sha256")
    if esperado:
        on_progress(1.0, f"Verificando el modelo «{name}»…")
        real = sha256_of(found / "model.bin", cancel)
        if real != esperado:
            # Se borra para que el próximo intento lo baje de nuevo en vez de
            # aceptarlo: find_model() da por bueno cualquier model.bin presente.
            try:
                (found / "model.bin").unlink()
            except OSError:
                pass
            log.error("Hash de %s no coincide: %s (esperado %s)", name, real, esperado)
            raise RuntimeError(
                f"El modelo «{name}» descargado no coincide con el esperado y se "
                "descartó. Probá de nuevo; si se repite, avisá."
            )
    return found


def sha256_of(path: Path, cancel: threading.Event | None = None) -> str:
    """Hash de un archivo grande, por bloques, sin cargarlo entero en memoria."""
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for bloque in iter(lambda: fh.read(8 * 1024 * 1024), b""):
            if cancel is not None and cancel.is_set():
                raise Cancelled
            digest.update(bloque)
    return digest.hexdigest()


class _SinSalida:
    """Sumidero de texto para tqdm.

    En el .exe de ventana no hay consola: `sys.stdout` y `sys.stderr` son None.
    tqdm escribe en `sys.stderr` apenas se construye la barra, así que sin esto
    bajar un modelo muere con
    `AttributeError: 'NoneType' object has no attribute 'write'`.
    El porcentaje lo pinta la UI, así que la salida de texto no hace falta.
    """

    def write(self, texto=""):
        return len(texto)

    def flush(self):
        pass

    def isatty(self):
        return False


def _make_tqdm(on_progress, cancel: threading.Event, size_mb: int):
    """Adaptador de la barra de tqdm que usa huggingface_hub hacia nuestra UI."""
    from tqdm import tqdm as _tqdm

    class _UiTqdm(_tqdm):  # type: ignore[misc]
        def __init__(self, *args, **kwargs):
            kwargs["file"] = _SinSalida()
            super().__init__(*args, **kwargs)

        def update(self, n=1):
            if cancel.is_set():
                raise Cancelled
            result = super().update(n)
            if self.total:
                frac = min(self.n / self.total, 1.0)
                on_progress(frac, f"Descargando el modelo (≈{size_mb} MB) — {frac * 100:.0f}%")
            return result

    return _UiTqdm


# --------------------------------------------------------------------------
# Escritura de resultados
# --------------------------------------------------------------------------


def _timestamp(seconds: float, comma: bool = False) -> str:
    total_ms = int(round(seconds * 1000))
    h, rem = divmod(total_ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    sep = "," if comma else "."
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def _mmss(seconds: float) -> str:
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class _Writer:
    """Va escribiendo mientras transcribe.

    Escribir sobre la marcha en vez de acumular en memoria significa que si se
    corta la luz a los 40 minutos, el .txt tiene los primeros 40 minutos.
    """

    # Un silencio de más de este tamaño se lee como cambio de tema y abre
    # párrafo nuevo. Sin esto el .txt limpio es un muro de texto ilegible.
    PARAGRAPH_GAP_S = 2.5

    def __init__(self, path: Path, fmt: str, mark_every_min: int = 0):
        self.path = path
        self.fmt = fmt
        self.fh = path.open("w", encoding="utf-8")
        self.index = 0
        self.prev_end: float | None = None
        self.line_len = 0
        # Marcas cada tantos minutos en el texto limpio: sirven para ubicar un
        # momento de la clase sin llenar cada línea de números.
        self.mark_every_s = max(0, int(mark_every_min)) * 60
        self.next_mark = self.mark_every_s if self.mark_every_s else None

    def add(self, segment) -> None:
        text = segment.text.strip()
        if not text:
            return
        self.index += 1

        if self.fmt == "srt":
            self.fh.write(
                f"{self.index}\n"
                f"{_timestamp(segment.start, comma=True)} --> "
                f"{_timestamp(segment.end, comma=True)}\n"
                f"{text}\n\n"
            )
        elif self.fmt == "txt_timestamps":
            self.fh.write(f"[{_mmss(segment.start)}] {text}\n")
        else:
            # La marca se pone al cruzar el minuto redondo, y arranca párrafo:
            # intercalarla dentro de una frase la partiría al medio.
            marca = None
            if self.next_mark is not None and segment.start >= self.next_mark:
                marca = int(segment.start // self.mark_every_s) * self.mark_every_s
                while self.next_mark <= segment.start:
                    self.next_mark += self.mark_every_s

            gap = segment.start - self.prev_end if self.prev_end is not None else 0.0
            if marca is not None:
                if self.prev_end is not None:
                    self.fh.write("\n\n")
                self.fh.write(f"[{_mmss(marca)}]\n")
                self.line_len = 0
            elif self.prev_end is None:
                pass
            elif gap > self.PARAGRAPH_GAP_S or self.line_len > 500:
                self.fh.write("\n\n")
                self.line_len = 0
            else:
                self.fh.write(" ")
            self.fh.write(text)
            self.line_len += len(text) + 1

        self.prev_end = segment.end
        self.fh.flush()

    def close(self) -> None:
        try:
            if self.fmt == "txt" and self.index:
                self.fh.write("\n")
            self.fh.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# Trabajo de transcripción
# --------------------------------------------------------------------------


@dataclass
class Job:
    source: Path
    output: Path
    model: str
    language: str
    cpu_profile: str
    output_format: str
    use_gpu: bool
    # Cada cuántos minutos dejar una marca de tiempo en el texto limpio.
    # 0 = ninguna, que es como se comportó siempre.
    mark_every_min: int = 0


@dataclass
class Result:
    output: Path
    audio_seconds: float
    elapsed_seconds: float
    device: str
    segments: int

    @property
    def speed(self) -> float:
        return self.audio_seconds / self.elapsed_seconds if self.elapsed_seconds else 0.0


@dataclass
class Callbacks:
    on_progress: object = field(default=lambda frac, text: None)
    on_preview: object = field(default=lambda text: None)
    on_done: object = field(default=lambda result: None)
    on_error: object = field(default=lambda exc: None)
    on_cancelled: object = field(default=lambda: None)


_model_cache: dict[tuple, object] = {}


def _load_model(name: str, device: str, threads: int):
    """Se cachea el modelo cargado: transcribir tres audios seguidos no debería
    releer 500 MB de disco cada vez."""
    key = (name, device, threads)
    cached = _model_cache.get(key)
    if cached is not None:
        return cached

    from faster_whisper import WhisperModel

    model_path = paths.find_model(name)
    if model_path is None:
        raise RuntimeError(f"No se encontró el modelo «{name}».")

    compute_type = "float16" if device == "cuda" else "int8"
    model = WhisperModel(
        str(model_path),
        device=device,
        compute_type=compute_type,
        cpu_threads=threads,
        num_workers=1,
    )
    _model_cache.clear()  # Un modelo a la vez: no tiene sentido tener dos en RAM.
    _model_cache[key] = model
    return model


class Runner(threading.Thread):
    """Corre un `Job` en su propio hilo.

    CTranslate2 libera el GIL mientras calcula, así que la ventana de Tkinter
    sigue respondiendo (y el botón Cancelar funciona) durante toda la corrida.
    """

    def __init__(self, job: Job, callbacks: Callbacks):
        super().__init__(daemon=True, name="MyWhisper-transcribe")
        self.job = job
        self.cb = callbacks
        self.cancel_event = threading.Event()

    def cancel(self) -> None:
        self.cancel_event.set()

    def run(self) -> None:
        try:
            self.cb.on_done(self._transcribe())
        except Cancelled:
            self.cb.on_cancelled()
        except Exception as exc:  # noqa: BLE001 — la UI muestra el mensaje
            log.exception("Falló la transcripción")
            self.cb.on_error(exc)
        finally:
            restore_priority()

    def _transcribe(self) -> Result:
        job = self.job
        cancel = self.cancel_event
        threads = apply_cpu_profile(job.cpu_profile)

        self.cb.on_progress(0.0, "Preparando…")
        ensure_model(job.model, self.cb.on_progress, cancel)
        if cancel.is_set():
            raise Cancelled

        device = "cuda" if job.use_gpu else "cpu"
        self.cb.on_progress(0.0, f"Cargando el modelo «{job.model}»…")
        try:
            model = _load_model(job.model, device, threads)
        except Exception as exc:
            if device != "cuda":
                raise
            # La GPU puede fallar por mil razones en la máquina de otro (driver
            # viejo, VRAM ocupada, pack incompleto). Caer a CPU es infinitamente
            # mejor que tirarle un error de CUDA a alguien que no programa.
            log.warning("Falló la GPU, se sigue en CPU: %s", exc)
            self.cb.on_progress(0.0, "La GPU no respondió; continuando en CPU…")
            device = "cpu"
            model = _load_model(job.model, device, threads)

        if cancel.is_set():
            raise Cancelled

        # En GPU sobra capacidad para búsqueda por haces; en CPU, greedy es casi
        # el doble de rápido y en audio de clase la diferencia apenas se nota.
        beam_size = 5 if device == "cuda" else 1

        self.cb.on_progress(0.0, "Analizando el audio…")
        started = time.monotonic()
        segments, info = model.transcribe(
            str(job.source),
            language=None if job.language == "auto" else job.language,
            beam_size=beam_size,
            # El VAD saltea los silencios. En grabaciones de clase, con sus
            # pausas largas, es de lejos la optimización que más rinde.
            vad_filter=True,
        )

        duration = float(getattr(info, "duration", 0.0) or 0.0)
        job.output.parent.mkdir(parents=True, exist_ok=True)
        writer = _Writer(job.output, job.output_format, job.mark_every_min)

        count = 0
        try:
            for segment in segments:
                if cancel.is_set():
                    raise Cancelled
                writer.add(segment)
                count += 1
                if duration:
                    frac = min(segment.end / duration, 1.0)
                    restante = _eta(started, frac)
                    self.cb.on_progress(
                        frac,
                        f"Transcribiendo — {frac * 100:.0f}%"
                        + (f" · quedan ~{restante}" if restante else ""),
                    )
                self.cb.on_preview(segment.text.strip())
        finally:
            writer.close()

        elapsed = time.monotonic() - started
        self.cb.on_progress(1.0, "Listo")
        return Result(
            output=job.output,
            audio_seconds=duration,
            elapsed_seconds=elapsed,
            device=device,
            segments=count,
        )


def _eta(started: float, fraction: float) -> str:
    if fraction <= 0.02:
        return ""
    elapsed = time.monotonic() - started
    remaining = elapsed / fraction - elapsed
    if remaining < 60:
        return f"{int(remaining)} s"
    if remaining < 3600:
        return f"{int(remaining // 60)} min"
    return f"{remaining / 3600:.1f} h"


def suggest_output(source: Path, fmt: str) -> Path:
    """El archivo sale AL LADO del audio, con su mismo nombre.

    Es la respuesta al "¿dónde quedó mi archivo?": si arrastraste algo desde
    Descargas, el texto aparece en Descargas.
    """
    ext = ".srt" if fmt == "srt" else ".txt"
    return source.with_name(source.stem + ext)
