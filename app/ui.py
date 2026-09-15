"""Interfaz gráfica.

Tkinter a propósito: viene en la biblioteca estándar de Python, PyInstaller la
empaqueta sin dramas y no agrega 200 MB de Qt al ZIP. La única dependencia
externa es `tkinterdnd2`, que aporta el arrastrar-y-soltar dentro de la ventana
— y si falta, la app funciona igual (queda el botón de elegir archivo, y
arrastrar sobre el ícono del .exe sigue andando porque eso lo resuelve Windows).

Regla del módulo: acá no se calcula nada. Todo el trabajo pesado vive en
`engine`, corre en otro hilo, y vuelve por `root.after(0, ...)`. Tkinter no es
thread-safe: tocar un widget desde el hilo de transcripción cuelga la ventana
de formas muy difíciles de reproducir.
"""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import engine, gpu, paths, settings as settings_mod

log = logging.getLogger(__name__)

# Paleta oscura, alineada con el tema sv-ttk (Sun Valley, el look de Windows 11)
# que se aplica en `_build_styles`. sv-ttk pinta solo los widgets ttk; estos
# colores son para los tk crudos —la zona de arrastre— y para los textos de
# color propio, que si no quedarían negros sobre fondo oscuro.
BG = "#1C1C1C"
CARD = "#2B2B2B"
INK = "#E8E8E8"
MUTED = "#9AA0A6"
ACCENT = "#57A6FF"
ACCENT_SOFT = "#243447"
BORDER = "#3A3A3A"
OK = "#3FB950"
WARN = "#E3A008"

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD

    _DND = True
except Exception:  # pragma: no cover - depende del entorno
    _DND = False
    DND_FILES = None  # type: ignore[assignment]
    TkinterDnD = None  # type: ignore[assignment]


def make_root() -> tk.Tk:
    if _DND:
        try:
            return TkinterDnD.Tk()
        except Exception:
            log.warning("tkinterdnd2 no arrancó; se sigue sin arrastrar", exc_info=True)
    return tk.Tk()


def _human_size(path: Path) -> str:
    try:
        mb = path.stat().st_size / 1048576
    except OSError:
        return ""
    return f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{mb:.0f} MB"


def _es_escribible(carpeta: Path) -> bool:
    """Probar de verdad, no adivinar por permisos.

    En Windows los permisos declarados mienten seguido (OneDrive, unidades de
    red, carpetas del sistema). Escribir y borrar un archivo mínimo es la única
    respuesta confiable, y es mejor saberlo al elegir la carpeta que después de
    una hora de transcripción.
    """
    prueba = carpeta / ".mywhisper-prueba"
    try:
        prueba.write_text("", encoding="utf-8")
        prueba.unlink()
        return True
    except Exception:
        return False


def _open_in_explorer(path: Path) -> None:
    """Abre el Explorador con el archivo seleccionado."""
    try:
        if os.name == "nt":
            subprocess.Popen(["explorer", "/select,", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path.parent)])
    except Exception:
        log.warning("No se pudo abrir el explorador", exc_info=True)


class App:
    def __init__(self, root: tk.Tk, initial_file: Path | None = None):
        self.root = root
        self.cfg = settings_mod.load()
        self.source: Path | None = None
        self.runner: engine.Runner | None = None
        self.last_output: Path | None = None

        root.title("MyWhisper — audio a texto")
        root.configure(bg=BG)
        root.geometry("680x660")
        root.minsize(640, 620)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_styles()
        self._build_widgets()
        self._wire_dnd()
        self._dark_title_bar()
        self._sync_marks()

        if initial_file is not None:
            self.set_source(initial_file)
        else:
            # Sin audio todavía, pero si hay carpeta fija guardada conviene que
            # se vea desde el arranque.
            self._refresh_folder_label()
        self._refresh_gpu_line()

    # ------------------------------------------------------------------
    # Construcción
    # ------------------------------------------------------------------

    def _build_styles(self) -> None:
        style = ttk.Style(self.root)
        # sv-ttk trae los widgets de Windows 11 (bordes redondeados, foco con
        # acento). Si no está —entorno de desarrollo sin instalarlo—, se cae a
        # `clam` y la app se ve como antes, pero abre igual.
        self._tema_moderno = True
        try:
            import sv_ttk

            sv_ttk.set_theme("dark")
        except Exception:
            self._tema_moderno = False
            log.warning("sv-ttk no disponible; se usa el tema básico", exc_info=True)
            try:
                style.theme_use("clam")
            except tk.TclError:
                pass
            style.configure("TFrame", background=BG)
            style.configure("TLabel", background=BG, foreground=INK, font=("Segoe UI", 10))
            style.configure("TLabelframe", background=BG, bordercolor=BORDER)
            style.configure("TLabelframe.Label", background=BG, foreground=MUTED)

        # Lo que sigue va ENCIMA del tema: sólo tipografías y los colores
        # propios de la app. Los fondos se los deja a sv-ttk, que ya los maneja.
        style.configure("Card.TFrame", background=CARD)
        style.configure("Card.TLabel", background=CARD, foreground=INK, font=("Segoe UI", 10))
        style.configure("Title.TLabel", font=("Segoe UI Semibold", 17), foreground=INK)
        style.configure("Muted.TLabel", foreground=MUTED, font=("Segoe UI", 9))
        style.configure("CardMuted.TLabel", background=CARD, foreground=MUTED, font=("Segoe UI", 9))
        style.configure("Ok.TLabel", foreground=OK, font=("Segoe UI", 9))
        style.configure("Warn.TLabel", foreground=WARN, font=("Segoe UI", 9))
        style.configure("TButton", font=("Segoe UI", 10))
        style.configure("Link.TButton", font=("Segoe UI", 9), padding=(4, 2))
        # El nombre compuesto hace que herede de Accent.TButton, el azul claro
        # que sv-ttk reserva para la acción principal. Si el tema no cargó, ttk
        # resuelve contra TButton y queda un botón común: feo, no roto.
        style.configure("Go.Accent.TButton", font=("Segoe UI Semibold", 11), padding=(20, 10))
        # La barra se deja como la dibuja el tema. Forzarle grosor y colores acá
        # la partía en dos líneas finas: sv-ttk la arma con su propio relieve y
        # los overrides le sacan el relleno.
        if not self._tema_moderno:
            style.configure(
                "Bar.Horizontal.TProgressbar",
                troughcolor=BORDER,
                background=ACCENT,
                borderwidth=0,
                thickness=8,
            )

    def _dark_title_bar(self) -> None:
        """Barra de título oscura (Windows 10 2004+).

        Sin esto la ventana queda con una franja blanca arriba que delata que el
        modo oscuro es sólo pintura del contenido.
        """
        try:
            self.root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            valor = ctypes.c_int(1)
            # 20 es el atributo actual; 19 el de las compilaciones viejas.
            for attr in (20, 19):
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(valor), ctypes.sizeof(valor)
                )
        except Exception:
            pass

    def _build_widgets(self) -> None:
        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)

        ttk.Label(outer, text="MyWhisper", style="Title.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            outer,
            text="Convierte audio o video en texto. Todo se procesa en tu PC.",
            style="Muted.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(2, 14))

        # --- zona de arrastre -----------------------------------------
        self.drop = tk.Frame(
            outer, bg=CARD, highlightbackground=BORDER, highlightthickness=2, height=118
        )
        self.drop.grid(row=2, column=0, sticky="ew")
        self.drop.grid_propagate(False)
        self.drop.columnconfigure(0, weight=1)
        self.drop.rowconfigure(0, weight=1)

        inner = tk.Frame(self.drop, bg=CARD)
        inner.grid(row=0, column=0)
        titulo = "Arrastrá acá tu audio o video" if _DND else "Elegí tu audio o video"
        self.drop_title = tk.Label(
            inner, text=titulo, bg=CARD, fg=INK, font=("Segoe UI Semibold", 12)
        )
        self.drop_title.pack()
        self.drop_sub = tk.Label(
            inner,
            text="o hacé clic para buscarlo",
            bg=CARD,
            fg=MUTED,
            font=("Segoe UI", 9),
        )
        self.drop_sub.pack(pady=(3, 0))

        for widget in (self.drop, inner, self.drop_title, self.drop_sub):
            widget.bind("<Button-1>", lambda _e: self.pick_file())
            widget.configure(cursor="hand2")

        # --- nombre de salida -----------------------------------------
        salida = ttk.Frame(outer)
        salida.grid(row=3, column=0, sticky="ew", pady=(14, 0))
        salida.columnconfigure(1, weight=1)

        ttk.Label(salida, text="Guardar como").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.name_var = tk.StringVar()
        self.name_entry = ttk.Entry(salida, textvariable=self.name_var, font=("Segoe UI", 10))
        self.name_entry.grid(row=0, column=1, sticky="ew")
        self.ext_label = ttk.Label(salida, text=".txt", style="Muted.TLabel")
        self.ext_label.grid(row=0, column=2, sticky="w", padx=(6, 0))

        # Fila de la carpeta destino. El botón "Junto al audio" sólo aparece
        # cuando hay una carpeta fija puesta: si no, no tendría qué deshacer.
        carpeta = ttk.Frame(salida)
        carpeta.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(5, 0))
        carpeta.columnconfigure(0, weight=1)

        self.folder_label = ttk.Label(carpeta, text="", style="Muted.TLabel")
        self.folder_label.grid(row=0, column=0, sticky="w")

        self.folder_button = ttk.Button(
            carpeta, text="Cambiar…", width=10, command=self.choose_folder
        )
        self.folder_button.grid(row=0, column=1, sticky="e", padx=(8, 0))

        self.folder_reset = ttk.Button(
            carpeta, text="Junto al audio", width=14, command=self.reset_folder
        )
        self.folder_reset.grid(row=0, column=2, sticky="e", padx=(6, 0))
        self.folder_reset.grid_remove()

        # --- opciones --------------------------------------------------
        opciones = ttk.LabelFrame(outer, text=" Opciones ", padding=12)
        opciones.grid(row=4, column=0, sticky="ew", pady=(16, 0))
        opciones.columnconfigure(1, weight=1)
        opciones.columnconfigure(3, weight=1)

        self.lang_var = tk.StringVar(value=settings_mod.LANGUAGES[self.cfg.language])
        self.model_var = tk.StringVar(value=settings_mod.MODELS[self.cfg.model]["label"])
        self.speed_var = tk.StringVar(
            value=settings_mod.CPU_PROFILES[self.cfg.cpu_profile]["label"]
        )
        self.fmt_var = tk.StringVar(value=settings_mod.OUTPUT_FORMATS[self.cfg.output_format])

        self._combo(opciones, "Idioma", self.lang_var, settings_mod.LANGUAGES.values(), 0, 0)
        self._combo(
            opciones,
            "Calidad",
            self.model_var,
            [m["label"] for m in settings_mod.MODELS.values()],
            0,
            2,
        )
        self._combo(
            opciones,
            "Velocidad",
            self.speed_var,
            [p["label"] for p in settings_mod.CPU_PROFILES.values()],
            1,
            0,
        )
        self._combo(
            opciones, "Formato", self.fmt_var, settings_mod.OUTPUT_FORMATS.values(), 1, 2
        )
        self.fmt_var.trace_add("write", lambda *_: self._sync_extension())

        # Marcas cada tantos minutos. Sólo tienen sentido en el texto limpio:
        # el formato con marcas ya numera cada frase y el .srt es puro tiempo.
        marcas = ttk.Frame(opciones)
        marcas.grid(row=2, column=0, columnspan=4, sticky="w", pady=(12, 0))
        self.mark_var = tk.BooleanVar(value=self.cfg.mark_minutes > 0)
        self.mark_check = ttk.Checkbutton(
            marcas,
            text="Marcar el tiempo cada",
            variable=self.mark_var,
            command=self._sync_marks,
        )
        self.mark_check.grid(row=0, column=0, sticky="w")
        self.mark_min_var = tk.StringVar(
            value=str(self.cfg.mark_minutes or settings_mod.DEFAULT_MARK_MINUTES)
        )
        self.mark_combo = ttk.Combobox(
            marcas,
            textvariable=self.mark_min_var,
            values=[str(m) for m in settings_mod.MARK_INTERVALS],
            state="readonly",
            width=4,
        )
        self.mark_combo.grid(row=0, column=1, padx=(8, 6))
        self.mark_hint = ttk.Label(marcas, text="minutos", style="Muted.TLabel")
        self.mark_hint.grid(row=0, column=2, sticky="w")

        gpu_row = ttk.Frame(opciones)
        gpu_row.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(12, 0))
        gpu_row.columnconfigure(1, weight=1)
        self.gpu_button = ttk.Button(
            gpu_row, text="Acelerar con placa NVIDIA…", command=self.open_gpu_dialog
        )
        self.gpu_button.grid(row=0, column=0, sticky="w")
        self.gpu_status = ttk.Label(gpu_row, text="", style="Muted.TLabel")
        self.gpu_status.grid(row=0, column=1, sticky="w", padx=(10, 0))

        # --- progreso --------------------------------------------------
        progreso = ttk.Frame(outer)
        progreso.grid(row=5, column=0, sticky="ew", pady=(16, 0))
        progreso.columnconfigure(0, weight=1)

        self.bar = ttk.Progressbar(
            progreso, style="Bar.Horizontal.TProgressbar", maximum=1000, value=0
        )
        self.bar.grid(row=0, column=0, sticky="ew")
        self.status = ttk.Label(progreso, text="Esperando un archivo…", style="Muted.TLabel")
        self.status.grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.preview = ttk.Label(
            progreso, text="", style="Muted.TLabel", wraplength=520, justify="left"
        )
        self.preview.grid(row=2, column=0, sticky="w", pady=(2, 0))

        # --- acciones --------------------------------------------------
        acciones = ttk.Frame(outer)
        acciones.grid(row=6, column=0, sticky="ew", pady=(16, 0))
        acciones.columnconfigure(0, weight=1)

        self.open_button = ttk.Button(
            acciones, text="Abrir carpeta", command=self.open_output, state="disabled"
        )
        self.open_button.grid(row=0, column=0, sticky="w")
        self.cancel_button = ttk.Button(
            acciones, text="Cancelar", command=self.cancel, state="disabled"
        )
        self.cancel_button.grid(row=0, column=1, padx=(0, 8))
        self.go_button = ttk.Button(
            acciones, text="Transcribir", style="Go.Accent.TButton", command=self.start, state="disabled"
        )
        self.go_button.grid(row=0, column=2)

        outer.rowconfigure(5, weight=1)

    def _combo(self, parent, label, var, values, row, col) -> ttk.Combobox:
        ttk.Label(parent, text=label).grid(
            row=row, column=col, sticky="w", padx=(0, 8), pady=4
        )
        combo = ttk.Combobox(
            parent, textvariable=var, values=list(values), state="readonly", width=22
        )
        combo.grid(row=row, column=col + 1, sticky="ew", padx=(0, 18), pady=5)
        return combo

    def _wire_dnd(self) -> None:
        if not _DND:
            return
        try:
            self.drop.drop_target_register(DND_FILES)
            self.drop.dnd_bind("<<Drop>>", self._on_drop)
            self.root.drop_target_register(DND_FILES)
            self.root.dnd_bind("<<Drop>>", self._on_drop)
        except Exception:
            log.warning("No se pudo registrar el destino de arrastre", exc_info=True)

    # ------------------------------------------------------------------
    # Entrada de archivos
    # ------------------------------------------------------------------

    def _on_drop(self, event) -> None:
        try:
            items = self.root.tk.splitlist(event.data)
        except Exception:
            items = [event.data]
        if items:
            self.set_source(Path(str(items[0]).strip("{}")))

    def pick_file(self) -> None:
        if self.runner is not None:
            return
        patrones = " ".join(f"*{e}" for e in engine.AUDIO_EXTENSIONS)
        elegido = filedialog.askopenfilename(
            title="Elegí un audio o video",
            filetypes=[("Audio y video", patrones), ("Todos los archivos", "*.*")],
        )
        if elegido:
            self.set_source(Path(elegido))

    def set_source(self, path: Path) -> None:
        if not path.exists() or path.is_dir():
            messagebox.showwarning(
                "Archivo no válido", f"No pude abrir:\n{path}", parent=self.root
            )
            return

        self.source = path
        self.drop_title.configure(text=path.name)
        tamano = _human_size(path)
        self.drop_sub.configure(
            text=f"{tamano} · clic para cambiarlo" if tamano else "clic para cambiarlo"
        )
        self.name_var.set(path.stem)
        self._refresh_folder_label()
        self.status.configure(text="Listo para transcribir")
        self.go_button.configure(state="normal")
        self._sync_extension()

    def _sync_extension(self) -> None:
        self.ext_label.configure(text=".srt" if self._format_key() == "srt" else ".txt")
        self._sync_marks()

    def _sync_marks(self) -> None:
        """La casilla sólo aplica al texto limpio; en los otros formatos se apaga."""
        aplica = self._format_key() == "txt"
        self.mark_check.configure(state="normal" if aplica else "disabled")
        activo = aplica and self.mark_var.get()
        self.mark_combo.configure(state="readonly" if activo else "disabled")
        self.mark_hint.configure(
            text="minutos" if aplica else "minutos (sólo para el texto limpio)"
        )

    # ------------------------------------------------------------------
    # Carpeta de destino
    # ------------------------------------------------------------------

    def target_dir(self) -> Path | None:
        """Dónde va a quedar el archivo: la carpeta fija, o la del audio.

        Devuelve None sólo mientras no haya audio elegido y tampoco carpeta fija:
        ahí todavía no hay nada que mostrar.
        """
        if self.cfg.output_dir:
            carpeta = Path(self.cfg.output_dir)
            if carpeta.is_dir():
                return carpeta
            # Desapareció entre medio (pendrive, carpeta borrada): se avisa una
            # vez y se sigue con la del audio, sin bloquear el trabajo.
            log.warning("La carpeta fija ya no existe: %s", self.cfg.output_dir)
            self.cfg.output_dir = ""
            settings_mod.save(self.cfg)
        return self.source.parent if self.source is not None else None

    def _refresh_folder_label(self) -> None:
        destino = self.target_dir()
        if destino is None:
            self.folder_label.configure(text="")
        else:
            self.folder_label.configure(text=f"Se guardará en: {destino}")
        if self.cfg.output_dir:
            self.folder_reset.grid()
        else:
            self.folder_reset.grid_remove()

    def choose_folder(self) -> None:
        if self.runner is not None:
            return
        actual = self.target_dir()
        elegida = filedialog.askdirectory(
            title="¿En qué carpeta dejo las transcripciones?",
            initialdir=str(actual) if actual else None,
            mustexist=True,
        )
        if not elegida:
            return
        carpeta = Path(elegida)
        if not _es_escribible(carpeta):
            messagebox.showwarning(
                "Carpeta sin permiso",
                f"No puedo escribir en:\n{carpeta}\n\nElegí otra.",
                parent=self.root,
            )
            return
        self.cfg.output_dir = str(carpeta)
        settings_mod.save(self.cfg)
        self._refresh_folder_label()

    def reset_folder(self) -> None:
        if self.runner is not None:
            return
        self.cfg.output_dir = ""
        settings_mod.save(self.cfg)
        self._refresh_folder_label()

    # ------------------------------------------------------------------
    # Lectura de la UI
    # ------------------------------------------------------------------

    @staticmethod
    def _key_from_label(mapping: dict, label: str, fallback: str) -> str:
        for key, value in mapping.items():
            texto = value["label"] if isinstance(value, dict) else value
            if texto == label:
                return key
        return fallback

    def _format_key(self) -> str:
        return self._key_from_label(settings_mod.OUTPUT_FORMATS, self.fmt_var.get(), "txt")

    def _collect_settings(self) -> settings_mod.Settings:
        self.cfg.language = self._key_from_label(
            settings_mod.LANGUAGES, self.lang_var.get(), "es"
        )
        self.cfg.model = self._key_from_label(
            settings_mod.MODELS, self.model_var.get(), settings_mod.DEFAULT_MODEL
        )
        self.cfg.cpu_profile = self._key_from_label(
            settings_mod.CPU_PROFILES, self.speed_var.get(), settings_mod.DEFAULT_CPU_PROFILE
        )
        self.cfg.output_format = self._format_key()
        if self.cfg.output_format == "txt" and self.mark_var.get():
            try:
                self.cfg.mark_minutes = int(self.mark_min_var.get())
            except ValueError:
                self.cfg.mark_minutes = settings_mod.DEFAULT_MARK_MINUTES
        else:
            self.cfg.mark_minutes = 0
        settings_mod.save(self.cfg)
        return self.cfg

    # ------------------------------------------------------------------
    # Transcripción
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self.runner is not None or self.source is None:
            return

        cfg = self._collect_settings()
        nombre = self.name_var.get().strip() or self.source.stem
        # Nada de rutas ni caracteres prohibidos: el campo es un NOMBRE.
        for prohibido in '<>:"/\\|?*':
            nombre = nombre.replace(prohibido, "-")
        extension = ".srt" if cfg.output_format == "srt" else ".txt"
        carpeta = self.target_dir() or self.source.parent
        destino = carpeta / (nombre + extension)

        if destino.exists():
            if not messagebox.askyesno(
                "Ya existe",
                f"«{destino.name}» ya existe en esa carpeta.\n¿Lo reemplazo?",
                parent=self.root,
            ):
                return
        if destino.resolve() == self.source.resolve():
            messagebox.showwarning(
                "Nombre en uso",
                "Ese nombre pisaría el audio original. Elegí otro.",
                parent=self.root,
            )
            return

        job = engine.Job(
            source=self.source,
            output=destino,
            model=cfg.model,
            language=cfg.language,
            cpu_profile=cfg.cpu_profile,
            output_format=cfg.output_format,
            mark_every_min=cfg.mark_minutes,
            use_gpu=cfg.use_gpu and gpu.pack_installed(),
        )

        callbacks = engine.Callbacks(
            on_progress=lambda f, t: self._ui(self._set_progress, f, t),
            on_preview=lambda t: self._ui(self.preview.configure, text=t),
            on_done=lambda r: self._ui(self._on_done, r),
            on_error=lambda e: self._ui(self._on_error, e),
            on_cancelled=lambda: self._ui(self._on_cancelled),
        )

        self.runner = engine.Runner(job, callbacks)
        self._set_busy(True)
        self.preview.configure(text="")
        self.runner.start()

    def cancel(self) -> None:
        if self.runner is not None:
            self.status.configure(text="Cancelando…")
            self.cancel_button.configure(state="disabled")
            self.runner.cancel()

    def _ui(self, fn, *args, **kwargs) -> None:
        """Único puente permitido entre el hilo de trabajo y los widgets."""
        try:
            self.root.after(0, lambda: fn(*args, **kwargs))
        except RuntimeError:
            pass  # La ventana ya se cerró.

    def _set_progress(self, fraction: float, text: str) -> None:
        self.bar.configure(value=max(0.0, min(fraction, 1.0)) * 1000)
        self.status.configure(text=text)

    def _set_busy(self, busy: bool) -> None:
        estado = "disabled" if busy else "normal"
        self.go_button.configure(state=estado if self.source else "disabled")
        self.cancel_button.configure(state="normal" if busy else "disabled")
        self.name_entry.configure(state=estado)
        self.gpu_button.configure(state=estado)
        self.drop.configure(cursor="watch" if busy else "hand2")

    def _on_done(self, result: engine.Result) -> None:
        self.runner = None
        self.last_output = result.output
        self._set_busy(False)
        self.bar.configure(value=1000)
        self.open_button.configure(state="normal")
        dispositivo = "GPU" if result.device == "cuda" else "CPU"
        minutos = result.elapsed_seconds / 60
        self.status.configure(
            text=f"Listo · {result.output.name} · {minutos:.1f} min en {dispositivo} "
            f"({result.speed:.1f}× tiempo real)"
        )
        self.preview.configure(text=f"Guardado en: {result.output.parent}")

    def _on_error(self, exc: Exception) -> None:
        self.runner = None
        self._set_busy(False)
        self.bar.configure(value=0)
        self.status.configure(text="No se pudo completar")
        messagebox.showerror(
            "Algo salió mal",
            f"{exc}\n\nEl detalle técnico quedó en:\n{paths.log_path()}",
            parent=self.root,
        )

    def _on_cancelled(self) -> None:
        self.runner = None
        self._set_busy(False)
        self.bar.configure(value=0)
        self.status.configure(text="Cancelado. El texto parcial quedó guardado.")

    def open_output(self) -> None:
        if self.last_output is not None:
            _open_in_explorer(self.last_output)

    def _on_close(self) -> None:
        if self.runner is not None and not messagebox.askyesno(
            "Hay una transcripción en curso",
            "¿Salir igual? Se conserva la parte ya transcrita.",
            parent=self.root,
        ):
            return
        if self.runner is not None:
            self.runner.cancel()
        settings_mod.save(self.cfg)
        self.root.destroy()

    # ------------------------------------------------------------------
    # GPU
    # ------------------------------------------------------------------

    def _refresh_gpu_line(self) -> None:
        if gpu.pack_installed() and self.cfg.use_gpu:
            self.gpu_status.configure(text="Aceleración NVIDIA activada", style="Ok.TLabel")
            self.gpu_button.configure(text="Aceleración NVIDIA…")
        elif gpu.pack_installed():
            self.gpu_status.configure(text="Pack instalado, desactivado", style="Muted.TLabel")
            self.gpu_button.configure(text="Aceleración NVIDIA…")
        else:
            self.gpu_status.configure(text="Usando CPU", style="Muted.TLabel")

    def open_gpu_dialog(self) -> None:
        GpuDialog(self)


class GpuDialog:
    """Ventana del pack CUDA.

    Está escrita para que alguien sin conocimientos técnicos entienda qué va a
    pasar ANTES de que pase: qué se descarga, de dónde, cuánto pesa, dónde
    queda y cómo se deshace. Nada acá pide permisos de administrador ni toca
    nada fuera de la carpeta de la app.
    """

    def __init__(self, app: App):
        self.app = app
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None

        self.win = tk.Toplevel(app.root)
        self.win.title("Acelerar con placa NVIDIA")
        self.win.configure(bg=BG)
        self.win.resizable(False, False)
        self.win.transient(app.root)
        self.win.grab_set()
        self.win.protocol("WM_DELETE_WINDOW", self._close)

        frame = ttk.Frame(self.win, padding=18)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="Aceleración por GPU", style="Title.TLabel").pack(anchor="w")

        self.info = ttk.Label(frame, text="Buscando placa…", wraplength=430, justify="left")
        self.info.pack(anchor="w", pady=(10, 0))

        self.detail = ttk.Label(
            frame, text="", style="Muted.TLabel", wraplength=430, justify="left"
        )
        self.detail.pack(anchor="w", pady=(8, 0))

        self.bar = ttk.Progressbar(
            frame, style="Bar.Horizontal.TProgressbar", maximum=1000, length=430
        )
        self.status = ttk.Label(frame, text="", style="Muted.TLabel", wraplength=430)

        self.enable_var = tk.BooleanVar(value=app.cfg.use_gpu)
        self.enable_check = ttk.Checkbutton(
            frame,
            text="Usar la GPU al transcribir",
            variable=self.enable_var,
            command=self._toggle_enable,
        )

        self.buttons = ttk.Frame(frame)
        self.buttons.pack(fill="x", pady=(18, 0))
        self.primary = ttk.Button(self.buttons, text="Cerrar", command=self._close)
        self.primary.pack(side="right")
        self.secondary = ttk.Button(self.buttons, text="", command=lambda: None)

        self.frame = frame
        self._center_on_parent()
        self.win.after(50, self._detect)

    def _center_on_parent(self) -> None:
        """Tkinter deja los Toplevel donde el gestor de ventanas quiera, que
        suele ser medio afuera de la ventana principal. Se centra a mano."""
        self.win.update_idletasks()
        raiz = self.app.root
        x = raiz.winfo_rootx() + (raiz.winfo_width() - self.win.winfo_width()) // 2
        y = raiz.winfo_rooty() + (raiz.winfo_height() - self.win.winfo_height()) // 3
        # Sin este tope, en un monitor chico el diálogo puede nacer con la
        # barra de título fuera de la pantalla y quedar imposible de mover.
        x = max(0, min(x, self.win.winfo_screenwidth() - self.win.winfo_width()))
        y = max(0, min(y, self.win.winfo_screenheight() - self.win.winfo_height()))
        self.win.geometry(f"+{x}+{y}")

    # -- estados -------------------------------------------------------

    def _detect(self) -> None:
        info = gpu.detect_nvidia()
        instalado = gpu.pack_installed()

        if info is None and not instalado:
            self.info.configure(
                text="No encontré ninguna placa NVIDIA en esta PC."
            )
            self.detail.configure(
                text="MyWhisper va a seguir usando el procesador, que funciona en "
                "cualquier equipo. Si sabés que tenés una placa NVIDIA, revisá que "
                "el driver esté instalado y volvé a abrir esta ventana."
            )
            return

        if instalado:
            self.info.configure(
                text=(f"Placa detectada: {info.name} ({info.vram_gb:.0f} GB)."
                      if info else "El pack de aceleración está instalado.")
            )
            self.detail.configure(
                text=f"Pack de aceleración instalado ({gpu.pack_size_mb()} MB) en:\n"
                f"{paths.cuda_dir()}\n\n"
                "Para desinstalarlo alcanza con borrar esa carpeta, o usar el botón "
                "de abajo."
            )
            self.enable_check.pack(anchor="w", pady=(14, 0))
            self.secondary.configure(text="Quitar el pack", command=self._remove)
            self.secondary.pack(side="right", padx=(0, 8))
            return

        gb_libre = _free_gb(paths.data_dir())
        self.info.configure(text=f"Placa detectada: {info.name} ({info.vram_gb:.0f} GB de VRAM).")
        self.detail.configure(
            text=(
                f"Con esta placa la transcripción es unas 3 veces más rápida — pero lo "
                f"que más cambia es la calidad: habilita los modelos grandes, que en "
                f"grabaciones ruidosas o con vocabulario técnico entienden lo que los "
                f"chicos no entienden.\n\n"
                f"Para usarla hay que descargar las librerías de NVIDIA, que no vienen "
                f"incluidas por su tamaño:\n\n"
                f"  • Se descargan {gpu.DOWNLOAD_MB} MB desde pypi.org, el repositorio "
                f"oficial de paquetes de Python.\n"
                f"  • Ocupan unos {gpu.INSTALLED_MB} MB en disco (tenés {gb_libre:.0f} GB libres).\n"
                f"  • Se verifica la firma SHA-256 de cada archivo antes de abrirlo.\n"
                f"  • Todo queda dentro de la carpeta de MyWhisper. No se instala nada "
                f"en Windows, no se piden permisos de administrador y no se toca el "
                f"driver de tu placa.\n"
                f"  • Para deshacerlo, se borra esa carpeta y listo."
            )
        )
        self.primary.configure(text="Descargar y activar", command=self._download)
        self.secondary.configure(text="Ahora no", command=self._close)
        self.secondary.pack(side="right", padx=(0, 8))

    def _toggle_enable(self) -> None:
        self.app.cfg.use_gpu = bool(self.enable_var.get())
        settings_mod.save(self.app.cfg)
        self.app._refresh_gpu_line()

    def _remove(self) -> None:
        if not messagebox.askyesno(
            "Quitar el pack",
            f"Se va a borrar la carpeta:\n{paths.cuda_dir()}\n\n"
            "MyWhisper va a volver a usar el procesador. ¿Continuar?",
            parent=self.win,
        ):
            return
        gpu.remove_pack()
        self.app.cfg.use_gpu = False
        settings_mod.save(self.app.cfg)
        self.app._refresh_gpu_line()
        messagebox.showinfo("Listo", "El pack se quitó.", parent=self.win)
        self._close()

    def _download(self) -> None:
        self.primary.configure(state="disabled")
        self.secondary.configure(text="Cancelar", command=self._request_cancel)
        self.bar.pack(fill="x", pady=(16, 0))
        self.status.pack(anchor="w", pady=(6, 0))
        self.status.configure(text="Conectando con pypi.org…")

        def trabajo() -> None:
            try:
                gpu.download_pack(
                    lambda f, t: self._ui(self._progress, f, t), self.cancel_event
                )
                self._ui(self._download_ok)
            except gpu.CancelledError:
                self._ui(self._download_cancelled)
            except Exception as exc:  # noqa: BLE001
                log.exception("Falló la descarga del pack CUDA")
                self._ui(self._download_failed, exc)

        self.worker = threading.Thread(target=trabajo, daemon=True, name="MyWhisper-cuda")
        self.worker.start()

    def _request_cancel(self) -> None:
        self.cancel_event.set()
        self.status.configure(text="Cancelando…")

    # -- callbacks del hilo --------------------------------------------

    def _ui(self, fn, *args) -> None:
        try:
            self.win.after(0, lambda: fn(*args))
        except (RuntimeError, tk.TclError):
            pass

    def _progress(self, fraction: float, text: str) -> None:
        self.bar.configure(value=max(0.0, min(fraction, 1.0)) * 1000)
        self.status.configure(text=text)

    def _download_ok(self) -> None:
        # `activate()` sólo sirve antes de que se cargue la DLL del motor. Si el
        # usuario ya transcribió algo en esta sesión, ctranslate2 está cargado
        # sin las rutas CUDA y no hay forma de agregarlas en caliente.
        gpu.activate()
        ok, mensaje = gpu.cuda_is_working()
        if not ok:
            self.status.configure(text=mensaje, style="Warn.TLabel")
            self.detail.configure(
                text="El pack quedó instalado, pero CUDA todavía no responde. "
                "Cerrá y volvé a abrir MyWhisper: las librerías se registran al "
                "arrancar."
            )
            self.app.cfg.use_gpu = True
            settings_mod.save(self.app.cfg)
            self.primary.configure(text="Cerrar", command=self._close, state="normal")
            self.secondary.pack_forget()
            return

        self.app.cfg.use_gpu = True
        settings_mod.save(self.app.cfg)
        self.app._refresh_gpu_line()
        self.status.configure(text=mensaje, style="Ok.TLabel")
        self.detail.configure(
            text=f"Aceleración activada. Todo quedó en:\n{paths.cuda_dir()}\n\n"
            "Si alguna vez querés volver atrás, borrá esa carpeta."
        )
        self.bar.configure(value=1000)
        self.primary.configure(text="Cerrar", command=self._close, state="normal")
        self.secondary.pack_forget()

    def _download_cancelled(self) -> None:
        self.status.configure(text="Descarga cancelada. No quedó nada a medias.")
        self.bar.configure(value=0)
        self.primary.configure(text="Cerrar", command=self._close, state="normal")
        self.secondary.pack_forget()

    def _download_failed(self, exc: Exception) -> None:
        self.status.configure(text="No se pudo completar la descarga.", style="Warn.TLabel")
        self.bar.configure(value=0)
        self.primary.configure(text="Cerrar", command=self._close, state="normal")
        self.secondary.pack_forget()
        messagebox.showerror(
            "Descarga fallida",
            f"{exc}\n\nMyWhisper sigue funcionando con el procesador.",
            parent=self.win,
        )

    def _close(self) -> None:
        self.cancel_event.set()
        try:
            self.win.grab_release()
        except tk.TclError:
            pass
        self.app._refresh_gpu_line()
        self.win.destroy()


def _free_gb(path: Path) -> float:
    try:
        import shutil as _shutil

        return _shutil.disk_usage(path).free / 1024**3
    except Exception:
        return 0.0
