"""PyInstaller entry point for the ee desktop backend (runs the `ee` CLI, e.g. `ee-backend serve`)."""
import sys

from ee.cli import main

if __name__ == "__main__":
    sys.exit(main())
