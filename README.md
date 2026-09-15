# MyWhisper

Convierte audio y video en texto, **en tu propio computador**. Nada se sube a internet.

App de escritorio para Windows basada en [faster-whisper](https://github.com/SYSTRAN/faster-whisper), el modelo Whisper de OpenAI. Portable: se descomprime y se usa, sin instalar nada.

> Versión 0.1.0-beta

## Características

- **Arrastra y suelta** cualquier audio o video: mp3, m4a, wav, mp4, mkv y más.
- **Tres formatos:** texto limpio, texto con tiempos por frase, o subtítulos `.srt`.
- **Marcas de tiempo cada 1, 2, 5 o 10 minutos**, para ubicar partes de una clase.
- **Tres calidades:** Estándar viene incluida; Alta y Máxima se descargan si las eliges.
- **Aceleración NVIDIA opcional**, sin instalar drivers ni pedir permisos de administrador.
- **Elige dónde guardar** los textos, o déjalos junto al audio.
- **No pierde trabajo:** si se corta la luz, lo transcrito hasta ese momento queda guardado.
- Tema oscuro y no acapara el procesador mientras trabaja.

## Privacidad

Solo se conecta a internet si **tú** eliges descargar un modelo más grande o la aceleración NVIDIA. Todo lo que descarga está verificado con su huella SHA-256. Sin telemetría ni envío de datos.

## Desarrollo

Windows y Python 3.11.

```powershell
py -3.11 -m venv buildenv
.\buildenv\Scripts\pip install -r requirements.txt
.\buildenv\Scripts\python run.py                            # abrir la app
.\buildenv\Scripts\python -m unittest discover -s tests     # pruebas
.\build.ps1                                                 # compilar y armar el ZIP
```

## Estructura

| Carpeta | Contenido |
|---|---|
| `app/` | Código: interfaz, motor de transcripción, GPU y configuración |
| `tests/` | Pruebas automáticas (sin GPU ni internet) |
| `tools/` | Ícono y recolección de licencias al compilar |
| `licencias-terceros/` | Licencias de los componentes que se distribuyen |
| `assets/LEEME.txt` | Instrucciones para quien recibe el programa |

## Licencia

Sin licencia por ahora: todos los derechos reservados.

Los componentes de terceros incluidos al compilar (FFmpeg, x264, x265, el modelo Whisper y otros) conservan sus propias licencias. Ver [`licencias-terceros/AVISOS.txt`](licencias-terceros/AVISOS.txt).
