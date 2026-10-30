"""Run from the checkout, without installing any third-party packages."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from rotation_bot.cli import main

if __name__ == "__main__":
    main()
