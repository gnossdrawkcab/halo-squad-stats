from pathlib import Path
import os

DATA_DIR = Path(os.getenv("HALO_DATA_DIR", ".")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)


def data_path(name: str) -> Path:
    return DATA_DIR / name
