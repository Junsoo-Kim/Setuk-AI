from __future__ import annotations

import sys
from pathlib import Path

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from version_C.server import app

    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
