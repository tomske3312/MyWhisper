"""Genera assets/MyWhisper.ico sin dependencias externas.

Pillow resolvería esto en cuatro líneas, pero sería una dependencia de 3 MB
para un archivo que se genera una vez. PNG e ICO son formatos simples y `zlib`
viene en la biblioteca estándar, así que se escriben a mano.

Dibujo: cuadrado redondeado azul con una onda de audio en blanco. Se renderiza
a 4x y se promedia para que los bordes queden suaves (antialiasing por
supersampling), porque no hay librería de dibujo que lo haga por nosotros.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

SS = 4  # factor de supersampling
BASE = 256
FONDO = (45, 107, 228)  # ACCENT de la interfaz
BARRA = (255, 255, 255)

# Alturas relativas de las barras de la onda, de 0 a 1.
ONDA = (0.30, 0.58, 0.86, 1.00, 0.72, 0.44, 0.66, 0.92, 0.52, 0.26)


def _dibujar(size: int) -> list[list[tuple[int, int, int, int]]]:
    n = size * SS
    radio = n * 0.22
    px = [[(0, 0, 0, 0)] * n for _ in range(n)]

    for y in range(n):
        for x in range(n):
            # Esquinas redondeadas: distancia al rectángulo interior.
            dx = max(radio - x, 0, x - (n - 1 - radio))
            dy = max(radio - y, 0, y - (n - 1 - radio))
            if dx * dx + dy * dy <= radio * radio:
                px[y][x] = (*FONDO, 255)

    # Onda centrada, con las barras redondeadas por el mismo criterio.
    margen = n * 0.17
    ancho_util = n - 2 * margen
    paso = ancho_util / len(ONDA)
    grosor = paso * 0.46
    centro = n / 2

    for i, altura in enumerate(ONDA):
        cx = margen + paso * (i + 0.5)
        media = (n * 0.32) * altura
        x0, x1 = int(cx - grosor / 2), int(cx + grosor / 2)
        y0, y1 = int(centro - media), int(centro + media)
        r = grosor / 2
        for y in range(max(y0, 0), min(y1 + 1, n)):
            for x in range(max(x0, 0), min(x1 + 1, n)):
                ddy = max(y0 + r - y, 0, y - (y1 - r))
                ddx = abs(x - cx)
                if ddx * ddx + ddy * ddy <= r * r:
                    px[y][x] = (*BARRA, 255)
    return px


def _reducir(px, size: int) -> bytes:
    """Promedia cada bloque SS x SS. Acá aparece el antialiasing."""
    filas = bytearray()
    total = SS * SS
    for y in range(size):
        filas.append(0)  # byte de filtro PNG "None"
        for x in range(size):
            r = g = b = a = 0
            for sy in range(SS):
                for sx in range(SS):
                    pr, pg, pb, pa = px[y * SS + sy][x * SS + sx]
                    r += pr * pa
                    g += pg * pa
                    b += pb * pa
                    a += pa
            if a:
                filas += bytes((r // a, g // a, b // a, a // total))
            else:
                filas += b"\x00\x00\x00\x00"
    return bytes(filas)


def _chunk(tipo: bytes, datos: bytes) -> bytes:
    return (
        struct.pack(">I", len(datos))
        + tipo
        + datos
        + struct.pack(">I", zlib.crc32(tipo + datos) & 0xFFFFFFFF)
    )


def _png(raw: bytes, size: int) -> bytes:
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(raw, 9))
        + _chunk(b"IEND", b"")
    )


def main() -> None:
    tamanos = (256, 64, 48, 32, 16)
    lienzo = _dibujar(BASE)

    imagenes = []
    for size in tamanos:
        if size == BASE:
            fuente = lienzo
        else:
            # Re-render en vez de escalar: a 16 px, redibujar da mucho mejor
            # resultado que promediar el de 256.
            fuente = _dibujar(size)
        imagenes.append((size, _png(_reducir(fuente, size), size)))

    cabecera = struct.pack("<HHH", 0, 1, len(imagenes))
    offset = len(cabecera) + 16 * len(imagenes)
    entradas, cuerpos = b"", b""
    for size, blob in imagenes:
        dim = 0 if size >= 256 else size
        entradas += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
        cuerpos += blob

    destino = Path(__file__).resolve().parent.parent / "assets" / "MyWhisper.ico"
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_bytes(cabecera + entradas + cuerpos)
    print(f"{destino} · {destino.stat().st_size / 1024:.1f} KB · tamaños {tamanos}")


if __name__ == "__main__":
    main()
