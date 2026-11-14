"""PyInstaller entry point for perfbox.exe (no arguments -> web UI)."""
from perfbox.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
