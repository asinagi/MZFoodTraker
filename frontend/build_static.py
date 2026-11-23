"""Prepare only browser assets for presentation hosting; never copy server secrets."""
from pathlib import Path
import shutil
root = Path(__file__).resolve().parent
out = root / "dist"
if out.exists():
    shutil.rmtree(out)
shutil.copytree(root / "public", out)
for name in ("developer.html", "mobile-check.html"):
    (out / name).unlink(missing_ok=True)
print("Static app prepared in dist/")
