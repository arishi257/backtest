from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIT_SENSEX_SRC = PROJECT_ROOT.parent / "fit_sensex" / "src"

if FIT_SENSEX_SRC.exists():
    fit_sensex_src_text = str(FIT_SENSEX_SRC)
    if fit_sensex_src_text not in sys.path:
        sys.path.insert(0, fit_sensex_src_text)

