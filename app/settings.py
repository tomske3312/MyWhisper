"""Configuración persistente, en un JSON al lado del .exe.

Deliberadamente tonto: si el archivo está corrupto o tiene basura, se ignora y
se vuelve a los valores por defecto. Una config rota nunca debe impedir abrir
la app — el usuario no tiene forma de arreglarla a mano.
"""

import json
import logging
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from . import paths

log = logging.getLogger(__name__)

# Perfiles de uso de CPU. El objetivo es poder seguir trabajando mientras
# transcribe. Ver engine.apply_cpu_profile().
#
# MEDIDO (12 hilos lógicos, modelo small int8, audio real de clase):
#     6 hilos  -> 2.86x tiempo real
#    12 hilos  -> 2.48x tiempo real
#
# Usar TODOS los hilos lógicos es más lento, no más rápido: CTranslate2 satura
# el ancho de banda de memoria y las dos mitades de cada núcleo SMT se pelean
# por la misma unidad vectorial. Por eso ningún perfil pasa de la mitad de los
# hilos — arriba de ahí sólo se pierde. La diferencia real entre "equilibrado"
# y "máximo" es la prioridad del proceso, no la cantidad de hilos.
CPU_PROFILES = {
    "silencioso": {
        "label": "Silencioso (de fondo)",
        "core_fraction": 0.25,
        "below_normal": True,
    },
    "equilibrado": {
        "label": "Equilibrado (recomendado)",
        "core_fraction": 0.5,
        "below_normal": True,
    },
    "maximo": {
        "label": "Máximo (no uses el PC)",
        "core_fraction": 0.5,
        "below_normal": False,
    },
}
DEFAULT_CPU_PROFILE = "equilibrado"

# Sólo `small` viaja dentro del paquete; los otros dos se bajan a pedido.
#
# `base` estaba y se sacó: sobre una grabación de clase real devolvió
# "20% 2% 2% 2%" donde el profesor decía "séptimo ciclo de reloj". Ahorra
# 142 MB de ZIP ofrecer una opción que no sirve, pero cuesta la confianza del
# que la prueba primero.
#
# MEDIDO sobre esa misma grabación (RTX 4060 / 6 hilos de CPU):
#   small    CPU  6.7x   "el 6º y el 9º 7º, el 9º, el 9º"
#   medium   GPU 16.4x   "el siglo XII, el cariño del llano"
#   large-v3 GPU 10.1x   "séptimo ciclo de reloj, séptimo"   <- lo correcto
#
# O sea: en audio difícil el tamaño del modelo pesa mucho más que cualquier
# otro ajuste. Por eso la GPU importa — no tanto por velocidad como por hacer
# viable el modelo que de verdad entiende.
#
# `revision` y `sha256` fijan exactamente QUÉ se descarga, con el mismo criterio
# que el pack CUDA en gpu.py: sin revisión fija se bajaría lo que haya hoy en la
# rama principal del repositorio, y el mismo botón podría traer mañana otro
# archivo. El commit identifica el contenido; el hash de model.bin lo comprueba
# después de bajarlo. Sacados de la API de Hugging Face (tree/<commit>).
MODELS = {
    "small": {
        "label": "Estándar (rápida)",
        "size_mb": 464,
        "revision": "536b0662742c02347bc0e980a01041f333bce120",
        "sha256": "3e305921506d8872816023e4c273e75d2419fb89b24da97b4fe7bce14170d671",
    },
    "medium": {
        "label": "Alta (audio difícil)",
        "size_mb": 1530,
        "revision": "08e178d48790749d25932bbc082711ddcfdfbc4f",
        "sha256": "9b45e1009dcc4ab601eff815b61d80e60ce3fd8c74c1a14f4a282258286b51ae",
    },
    "large-v3": {
        "label": "Máxima (pide GPU)",
        "size_mb": 3090,
        "revision": "edaa852ec7e145841d8ffdb056a99866b5f0a478",
        "sha256": "69f74147e3334731bc3a76048724833325d2ec74642fb52620eda87352e3d4f1",
    },
}
DEFAULT_MODEL = "small"

LANGUAGES = {
    "es": "Español",
    "en": "Inglés",
    "pt": "Portugués",
    "fr": "Francés",
    "de": "Alemán",
    "it": "Italiano",
    "auto": "Detectar automáticamente",
}

# Cada cuánto dejar una marca de tiempo en el texto limpio. Son minutos
# redondos: suficientes para volver a un punto de la clase, sin el ruido de
# numerar cada frase (para eso ya está el formato con marcas en todas).
MARK_INTERVALS = (1, 2, 5, 10)
DEFAULT_MARK_MINUTES = 5

OUTPUT_FORMATS = {
    "txt": "Texto limpio (.txt)",
    "txt_timestamps": "Texto con marcas de tiempo (.txt)",
    "srt": "Subtítulos (.srt)",
}


@dataclass
class Settings:
    model: str = DEFAULT_MODEL
    language: str = "es"
    cpu_profile: str = DEFAULT_CPU_PROFILE
    output_format: str = "txt"
    # Carpeta fija donde dejar las transcripciones. Vacía = junto al audio, que
    # es lo que la app hizo siempre y sigue siendo el valor por defecto.
    output_dir: str = ""
    # Marcas de tiempo cada tantos minutos en el texto limpio. 0 = ninguna.
    mark_minutes: int = 0
    use_gpu: bool = False
    # Se pone en True la primera vez que el usuario ve el aviso del pack CUDA,
    # para no repetir la explicación larga en cada arranque.
    cuda_notice_seen: bool = False

    def sanitize(self) -> "Settings":
        if self.model not in MODELS:
            self.model = DEFAULT_MODEL
        if self.language not in LANGUAGES:
            self.language = "es"
        if self.cpu_profile not in CPU_PROFILES:
            self.cpu_profile = DEFAULT_CPU_PROFILE
        if self.output_format not in OUTPUT_FORMATS:
            self.output_format = "txt"
        # Si la carpeta guardada ya no está (un pendrive que se sacó, una ruta
        # borrada), se vuelve en silencio a "junto al audio" en vez de fallar
        # recién a la hora de escribir el archivo.
        if self.output_dir:
            try:
                if not Path(self.output_dir).is_dir():
                    self.output_dir = ""
            except Exception:
                self.output_dir = ""
        try:
            self.mark_minutes = int(self.mark_minutes)
        except (TypeError, ValueError):
            self.mark_minutes = 0
        if self.mark_minutes not in MARK_INTERVALS:
            self.mark_minutes = 0
        self.use_gpu = bool(self.use_gpu)
        self.cuda_notice_seen = bool(self.cuda_notice_seen)
        return self


def load() -> Settings:
    path = paths.settings_path()
    if not path.exists():
        return Settings()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        known = {f.name for f in fields(Settings)}
        return Settings(**{k: v for k, v in raw.items() if k in known}).sanitize()
    except Exception:
        log.warning("Config ilegible, se usan valores por defecto", exc_info=True)
        return Settings()


def save(settings: Settings) -> None:
    try:
        paths.settings_path().write_text(
            json.dumps(asdict(settings), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        # Que no se pueda guardar la config no es motivo para romper nada.
        log.warning("No se pudo guardar la config", exc_info=True)
