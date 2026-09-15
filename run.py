"""Lanzador. PyInstaller necesita un script suelto como punto de entrada;
`python -m app.main` no le sirve."""

from app.main import main

if __name__ == "__main__":
    raise SystemExit(main())
