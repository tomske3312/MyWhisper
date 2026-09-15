"""Resolución de rutas.

La app tiene que funcionar igual corriendo desde fuentes (`python -m app.main`)
que empaquetada con PyInstaller en modo *onedir*. Todo lo que sea "¿dónde está
tal cosa?" pasa por acá para no repetir el `sys.frozen` en veinte lugares.

Se distingue entre:
  - `resource_dir()`  -> lo que viene DENTRO del paquete (modelos incluidos).
  - `data_dir()`      -> lo que la app escribe (config, log, modelos bajados,
                         pack CUDA). Normalmente es la carpeta del .exe, pero
                         cae a LOCALAPPDATA si esa carpeta es de solo lectura
                         (ej. alguien la descomprime en Archivos de programa).
"""

import os
import sys
import tempfile
from pathlib import Path

APP_NAME = "MyWhisper"


def is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def app_dir() -> Path:
    """Carpeta que el usuario ve: la del .exe, o la raíz del repo en desarrollo."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resource_dir() -> Path:
    """Recursos empaquetados. En onedir es `_internal/`, en fuentes es la raíz."""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", app_dir()))
    return app_dir()


def _is_writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".escritura_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        return False


_data_dir_cache: Path | None = None


def data_dir() -> Path:
    """Dónde escribimos. Preferimos la carpeta del .exe: la app es portable y
    que todo quede junto hace que borrarla la desinstale de verdad."""
    global _data_dir_cache
    if _data_dir_cache is not None:
        return _data_dir_cache

    candidate = app_dir()
    if not _is_writable(candidate):
        local = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
        candidate = Path(local) / APP_NAME
        candidate.mkdir(parents=True, exist_ok=True)

    _data_dir_cache = candidate
    return candidate


def model_search_paths(name: str) -> list[Path]:
    """Un modelo puede venir empaquetado o haber sido descargado después.
    Se busca primero el descargado: si alguien bajó uno mejor, gana ese."""
    return [data_dir() / "models" / name, resource_dir() / "models" / name]


def find_model(name: str) -> Path | None:
    for candidate in model_search_paths(name):
        if (candidate / "model.bin").exists():
            return candidate
    return None


def downloaded_models_dir() -> Path:
    return data_dir() / "models"


def cuda_dir() -> Path:
    return data_dir() / "cuda"


def settings_path() -> Path:
    """Config actual. Si sólo existe la de cuando la app se llamaba MiWhisper,
    se renombra una vez para no perder las preferencias del usuario."""
    actual = data_dir() / "MyWhisper.config.json"
    anterior = data_dir() / "MiWhisper.config.json"
    if not actual.exists() and anterior.exists():
        try:
            anterior.rename(actual)
        except OSError:
            return anterior
    return actual


def log_path() -> Path:
    return data_dir() / "MyWhisper.log"
