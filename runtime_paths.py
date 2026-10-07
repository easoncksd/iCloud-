"""One explicit data root; importing application modules never creates files."""
import os
from pathlib import Path

DATA_ROOT = Path(os.environ.get('ICLOUD_DATA_DIR', Path(__file__).resolve().parent)).resolve()
