"""Pruebas de la lógica que es fácil romper sin notarlo.

No necesitan GPU, modelos ni internet, y corren en un par de segundos:

    .\\buildenv\\Scripts\\python -m unittest discover -s tests -v

Se usa `unittest` de la biblioteca estándar a propósito: agregar pytest sería
una dependencia más sólo para esto.
"""

from __future__ import annotations

import dataclasses
import logging
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

from app import engine, settings as settings_mod  # noqa: E402
from app.main import _FormatoSinUsuario, ensure_std_streams  # noqa: E402


class _Segmento:
    def __init__(self, start: float, end: float, text: str):
        self.start, self.end, self.text = start, end, text


def _clase(minutos: int = 12, cada_s: int = 20) -> list[_Segmento]:
    """Una "clase" sintética: una frase cada `cada_s` segundos."""
    return [
        _Segmento(t, t + cada_s - 2, f"frase en el segundo {t}.")
        for t in range(0, minutos * 60, cada_s)
    ]


class _ConCarpetaTemporal(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="mywhisper-test-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


class MarcasDeTiempo(_ConCarpetaTemporal):
    def escribir(self, fmt: str, cada_min: int) -> str:
        ruta = self.tmp / f"salida-{fmt}-{cada_min}.txt"
        w = engine._Writer(ruta, fmt, cada_min)
        for s in _clase():
            w.add(s)
        w.close()
        return ruta.read_text(encoding="utf-8")

    def test_sin_marcas_no_agrega_nada(self):
        self.assertNotIn("[", self.escribir("txt", 0))

    def test_cada_5_minutos(self):
        texto = self.escribir("txt", 5)
        self.assertEqual(texto.count("["), 2)
        self.assertIn("[05:00]", texto)
        self.assertIn("[10:00]", texto)

    def test_cada_minuto(self):
        self.assertEqual(self.escribir("txt", 1).count("["), 11)

    def test_la_marca_abre_parrafo_antes_de_la_frase(self):
        texto = self.escribir("txt", 5)
        self.assertIn("\n\n[05:00]\nfrase en el segundo 300.", texto)

    def test_no_se_pierde_ninguna_frase(self):
        texto = self.escribir("txt", 5)
        for s in _clase():
            self.assertIn(s.text, texto)

    def test_srt_ignora_la_opcion(self):
        texto = self.escribir("srt", 5)
        self.assertIn("-->", texto)
        self.assertNotIn("[", texto)

    def test_formato_con_todas_las_marcas_ignora_la_opcion(self):
        self.assertEqual(self.escribir("txt_timestamps", 5).count("["), len(_clase()))


class Configuracion(_ConCarpetaTemporal):
    def test_carpeta_inexistente_vuelve_a_junto_al_audio(self):
        cfg = settings_mod.Settings(output_dir=str(self.tmp / "no-existe")).sanitize()
        self.assertEqual(cfg.output_dir, "")

    def test_carpeta_valida_se_conserva(self):
        cfg = settings_mod.Settings(output_dir=str(self.tmp)).sanitize()
        self.assertEqual(cfg.output_dir, str(self.tmp))

    def test_intervalo_de_marcas_invalido_se_apaga(self):
        self.assertEqual(settings_mod.Settings(mark_minutes=7).sanitize().mark_minutes, 0)
        self.assertEqual(settings_mod.Settings(mark_minutes="x").sanitize().mark_minutes, 0)

    def test_config_vieja_sin_campos_nuevos_carga(self):
        vieja = {"model": "small", "language": "es", "use_gpu": True}
        conocidos = {f.name for f in dataclasses.fields(settings_mod.Settings)}
        cfg = settings_mod.Settings(**{k: v for k, v in vieja.items() if k in conocidos}).sanitize()
        self.assertEqual((cfg.output_dir, cfg.mark_minutes, cfg.use_gpu), ("", 0, True))

    def test_modelo_desconocido_vuelve_al_default(self):
        cfg = settings_mod.Settings(model="base").sanitize()
        self.assertEqual(cfg.model, settings_mod.DEFAULT_MODEL)


class Modelos(unittest.TestCase):
    def test_todos_los_modelos_tienen_revision_y_hash_fijos(self):
        for nombre, spec in settings_mod.MODELS.items():
            with self.subTest(modelo=nombre):
                self.assertRegex(spec.get("revision", ""), r"^[0-9a-f]{40}$")
                self.assertRegex(spec.get("sha256", ""), r"^[0-9a-f]{64}$")


class HashDeArchivos(unittest.TestCase):
    def test_sha256_por_bloques_coincide_con_hashlib(self):
        import hashlib

        with tempfile.NamedTemporaryFile(delete=False) as fh:
            datos = b"mywhisper" * 2_000_000  # ~18 MB: cruza varios bloques
            fh.write(datos)
            ruta = Path(fh.name)
        try:
            self.assertEqual(engine.sha256_of(ruta), hashlib.sha256(datos).hexdigest())
        finally:
            ruta.unlink()

    def test_se_puede_cancelar(self):
        with tempfile.NamedTemporaryFile(delete=False) as fh:
            fh.write(b"x" * 1024)
            ruta = Path(fh.name)
        cancel = threading.Event()
        cancel.set()
        try:
            with self.assertRaises(engine.Cancelled):
                engine.sha256_of(ruta, cancel)
        finally:
            ruta.unlink()


class SinConsola(unittest.TestCase):
    """El .exe de ventana arranca con stdout/stderr en None."""

    def setUp(self) -> None:
        self._out, self._err = sys.stdout, sys.stderr

    def tearDown(self) -> None:
        sys.stdout, sys.stderr = self._out, self._err

    def test_la_barra_de_descarga_no_revienta(self):
        sys.stdout = sys.stderr = None
        barra = engine._make_tqdm(lambda f, t: None, threading.Event(), 100)(total=1000)
        barra.update(500)
        barra.close()

    def test_la_red_de_seguridad_repone_los_streams(self):
        sys.stdout = sys.stderr = None
        ensure_std_streams()
        print("no revienta")
        sys.stderr.write("tampoco\n")


class CarpetaDeDestino(_ConCarpetaTemporal):
    """La lógica de `App.target_dir` sin abrir una ventana."""

    class _AppFalsa:
        def __init__(self, cfg, source):
            self.cfg, self.source, self.runner = cfg, source, None

    def setUp(self) -> None:
        super().setUp()
        from app import ui

        self.ui = ui
        self._AppFalsa.target_dir = ui.App.target_dir
        self.audio = self.tmp / "clase.mp3"
        self.audio.write_bytes(b"")

    def test_carpeta_escribible_se_detecta_y_no_deja_basura(self):
        self.assertTrue(self.ui._es_escribible(self.tmp))
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["clase.mp3"])

    def test_carpeta_inexistente_no_es_escribible(self):
        self.assertFalse(self.ui._es_escribible(self.tmp / "no" / "existe"))

    def test_carpeta_fija_manda(self):
        fija = self.tmp / "fija"
        fija.mkdir()
        app = self._AppFalsa(settings_mod.Settings(output_dir=str(fija)), self.audio)
        self.assertEqual(app.target_dir(), fija)

    def test_sin_carpeta_fija_va_junto_al_audio(self):
        app = self._AppFalsa(settings_mod.Settings(), self.audio)
        self.assertEqual(app.target_dir(), self.tmp)

    def test_sin_audio_ni_carpeta_no_hay_destino(self):
        self.assertIsNone(self._AppFalsa(settings_mod.Settings(), None).target_dir())


class Log(unittest.TestCase):
    def test_enmascara_la_carpeta_del_usuario(self):
        home = str(Path.home())
        registro = logging.LogRecord("t", logging.INFO, __file__, 1, "datos en %s", (home + "\\x",), None)
        texto = _FormatoSinUsuario("%(message)s").format(registro)
        self.assertNotIn(home, texto)
        self.assertIn("%USERPROFILE%", texto)


if __name__ == "__main__":
    unittest.main()
