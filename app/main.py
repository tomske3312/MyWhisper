"""Punto de entrada.

El orden de arranque importa y no es intercambiable:

  1. Logging primero, para que cualquier cosa que explote después quede escrita.
  2. `gpu.activate()` ANTES de tocar nada del motor. Registra las rutas de las
     DLL de CUDA, y eso sólo tiene efecto si pasa antes de que Windows cargue la
     DLL de CTranslate2. Después ya es tarde.
  3. Recién entonces se arma la ventana.

Se puede invocar de dos maneras y las dos terminan en la misma ventana:
    MyWhisper.exe                  -> ventana vacía
    MyWhisper.exe "C:\\clase.m4a"   -> ventana con el archivo ya cargado
El segundo caso es el que dispara Windows cuando arrastrás un archivo sobre el
ícono del .exe, así que arrastrar funciona sin escribir una línea para eso.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import gpu, paths, settings as settings_mod

log = logging.getLogger("mywhisper")


def ensure_std_streams() -> None:
    """Red de seguridad: en el .exe de ventana `sys.stdout`/`sys.stderr` son None.

    Cualquier librería que imprima algo (tqdm, un aviso de huggingface_hub) tumba
    la app con `'NoneType' object has no attribute 'write'`. Se les da un destino
    nulo para que escribir no cueste nada y no rompa. Tiene que correr antes que
    todo lo demás.
    """
    for nombre in ("stdout", "stderr"):
        if getattr(sys, nombre, None) is None:
            setattr(sys, nombre, open(os.devnull, "w", encoding="utf-8"))


class _FormatoSinUsuario(logging.Formatter):
    """Reemplaza la carpeta del usuario por %USERPROFILE% en todo lo escrito.

    El log está pensado para mandarlo cuando algo falla, y cada ruta de Windows
    lleva el nombre de la cuenta (`C:\\Users\\<nombre>\\...`). Se enmascara en el
    texto ya formateado, así también cubre las rutas dentro de los tracebacks.
    """

    _home = str(Path.home())

    def format(self, record: logging.LogRecord) -> str:
        texto = super().format(record)
        if self._home and len(self._home) > 3:
            texto = texto.replace(self._home, "%USERPROFILE%")
        return texto


def setup_logging() -> None:
    """Un log rotativo al lado del .exe.

    En una beta que se le pasa a conocidos, "mandame el MyWhisper.log" es la
    diferencia entre poder arreglar algo y jugar a las adivinanzas.
    """
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    formato = _FormatoSinUsuario("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    try:
        handler = RotatingFileHandler(
            paths.log_path(), maxBytes=512_000, backupCount=1, encoding="utf-8"
        )
        handler.setFormatter(formato)
        root.addHandler(handler)
    except Exception:
        pass

    if not getattr(sys, "frozen", False):
        consola = logging.StreamHandler()
        consola.setFormatter(formato)
        root.addHandler(consola)


def enable_dpi_awareness() -> None:
    """Sin esto, Tkinter se ve borroso en cualquier pantalla con escalado."""
    if os.name != "nt":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _scale_for_dpi(root) -> None:
    try:
        dpi = root.winfo_fpixels("1i")
        root.tk.call("tk", "scaling", dpi / 72.0)
    except Exception:
        pass


def first_file_from_argv() -> Path | None:
    for arg in sys.argv[1:]:
        if arg.startswith("-"):
            continue
        candidate = Path(arg)
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def wants_diagnostic() -> bool:
    """Modo diagnóstico: sólo para desarrollo, `python run.py --diagnostico`.

    En el .exe se ignora. Es una app de ventana sin consola: lo que imprime el
    diagnóstico no se vería en ningún lado, y la app quedaría abierta sin ventana.
    """
    return "--diagnostico" in sys.argv and not paths.is_frozen()


def run_diagnostic(source: Path | None) -> int:
    """Modo sin ventana: `python run.py --diagnostico [archivo]`.

    Imprime qué encontró (placa, driver, pack CUDA, modelo) y, si le pasan un
    audio, transcribe de verdad para probar la cadena entera.
    """
    from . import __version__, engine

    print(f"MyWhisper {__version__}")
    print(f"  empaquetado : {paths.is_frozen()}")
    print(f"  datos       : {paths.data_dir()}")
    print(f"  modelo small: {paths.find_model('small')}")

    info = gpu.detect_nvidia()
    print(f"  placa NVIDIA: {info.name + f' ({info.vram_gb:.0f} GB, driver {info.driver})' if info else 'no detectada'}")
    print(f"  pack CUDA   : {'instalado (' + str(gpu.pack_size_mb()) + ' MB)' if gpu.pack_installed() else 'no instalado'}")
    if gpu.pack_installed():
        gpu.activate()
        print(f"  CUDA        : {gpu.cuda_is_working()[1]}")

    if source is None:
        print("\nPasale un audio para probar la transcripción completa.")
        return 0

    import threading

    cfg = settings_mod.load()
    destino = engine.suggest_output(source, "txt").with_name(source.stem + ".diagnostico.txt")
    job = engine.Job(
        source=source, output=destino, model=cfg.model, language=cfg.language,
        cpu_profile=cfg.cpu_profile, output_format="txt",
        use_gpu=cfg.use_gpu and gpu.pack_installed(),
    )
    listo, salida = threading.Event(), {}
    runner = engine.Runner(
        job,
        engine.Callbacks(
            on_progress=lambda f, t: print(f"\r  {t:<70}", end="", flush=True),
            on_preview=lambda t: None,
            on_done=lambda r: (salida.update(r=r), listo.set()),
            on_error=lambda e: (salida.update(e=e), listo.set()),
            on_cancelled=lambda: listo.set(),
        ),
    )
    print(f"\nTranscribiendo {source.name}…")
    runner.start()
    listo.wait()
    print()
    if "e" in salida:
        print(f"  FALLÓ: {salida['e']}")
        return 1
    r = salida["r"]
    print(f"  OK · {r.segments} segmentos · {r.speed:.1f}x tiempo real · {r.device}")
    print(f"  {r.output}")
    return 0


def main() -> int:
    ensure_std_streams()
    setup_logging()
    enable_dpi_awareness()
    log.info("MyWhisper arrancando · frozen=%s · datos=%s", paths.is_frozen(), paths.data_dir())

    cfg = settings_mod.load()
    if cfg.use_gpu and gpu.pack_installed():
        if gpu.activate():
            ok, mensaje = gpu.cuda_is_working()
            log.info("CUDA: %s", mensaje)
            if not ok:
                # No se apaga la preferencia del usuario: puede ser algo
                # transitorio (otra app ocupando la placa). El motor cae a CPU
                # solo cuando llegue el momento de transcribir.
                log.warning("El pack CUDA está pero no responde; se usará CPU.")

    if wants_diagnostic():
        return run_diagnostic(first_file_from_argv())

    # Se importa acá, después de activate(): importar `ui` arrastra `engine`.
    from .ui import App, make_root

    try:
        root = make_root()
        _scale_for_dpi(root)
        App(root, initial_file=first_file_from_argv())
        root.mainloop()
    except Exception:
        log.critical("Falla fatal al arrancar\n%s", traceback.format_exc())
        _show_fatal()
        return 1
    return 0


def _show_fatal() -> None:
    """Último recurso: si ni la ventana pudo abrirse, al menos decir dónde mirar."""
    try:
        ctypes.windll.user32.MessageBoxW(
            None,
            f"MyWhisper no pudo iniciarse.\n\nEl detalle quedó en:\n{paths.log_path()}",
            "MyWhisper",
            0x10,
        )
    except Exception:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
