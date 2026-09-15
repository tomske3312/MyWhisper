# -*- mode: python ; coding: utf-8 -*-
"""Receta de PyInstaller para MyWhisper.

Decisiones que no son obvias:

* **onedir, no onefile.** `--onefile` produce un .exe lindo de un solo archivo,
  pero en cada arranque descomprime ~300 MB a una carpeta temporal. Serían
  entre 5 y 15 segundos de espera antes de ver la ventana, cada vez, y encima
  es el patrón que más falsos positivos dispara en los antivirus. onedir abre
  al instante y el ZIP se distribuye igual de bien.

* **`collect_all` para las librerías con binarios.** faster-whisper trae el
  modelo VAD de Silero en `assets/`, ctranslate2 y onnxruntime traen sus DLL,
  y `av` sus binarios de FFmpeg. El análisis estático de PyInstaller no ve nada
  de eso: hay que juntarlo explícitamente o el .exe compila y falla al abrir.

* **Sin UPX.** Comprimir DLL con UPX ahorra unos 40 MB y a cambio dispara
  heurísticas de antivirus. En una beta que se pasa por Drive a conocidos, esa
  es exactamente la pelea que no queremos.
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all

HERE = Path(SPECPATH)

datas, binaries, hiddenimports = [], [], []

# Paquetes que esconden datos o DLL que el analizador no detecta solo.
for paquete in (
    "faster_whisper",   # incluye los modelos ONNX del VAD
    "ctranslate2",      # el motor de inferencia
    "onnxruntime",      # lo usa el VAD
    "av",               # FFmpeg embebido: decodifica mp3/m4a/mp4/mkv
    "tokenizers",
    "huggingface_hub",  # para bajar medium / large-v3 a pedido
    "tkinterdnd2",      # arrastrar y soltar dentro de la ventana
    "sv_ttk",           # tema Windows 11: son archivos .tcl, no código Python
):
    d, b, h = collect_all(paquete)
    datas += d
    binaries += b
    hiddenimports += h

# El modelo `small` viaja adentro: la app tiene que servir sin internet la
# primera vez que alguien la abre.
datas += [(str(HERE / "models"), "models")]

a = Analysis(
    ["run.py"],
    pathex=[str(HERE)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # Nada de esto se usa; sacarlo son ~80 MB menos.
        "matplotlib", "pandas", "scipy", "PIL", "IPython", "notebook",
        "pytest", "setuptools", "pip", "wheel", "torch", "transformers",
        "tkinter.test", "test",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

ICONO = str(HERE / "assets" / "MyWhisper.ico")

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MyWhisper",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # es una app de ventana: nada de consola negra de fondo
    disable_windowed_traceback=False,
    icon=ICONO,
)

# Segundo ejecutable, CON consola, para cuando algo falla en una máquina a la
# que no tengo acceso. Comparte el mismo COLLECT que el principal, así que sale
# gratis: son unos pocos MB de bootloader, no otra copia de las librerías.
# `main.py` entra en modo diagnóstico al ver este nombre en sys.executable.
exe_diagnostico = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MyWhisper-Diagnostico",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    icon=ICONO,
)

coll = COLLECT(
    exe,
    exe_diagnostico,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="MyWhisper",
)
