"""Create a timestamped backup of the local SQLite CRM database.
For PostgreSQL production deployments, use pg_dump instead.
"""
from pathlib import Path
from datetime import datetime
import shutil

root = Path(__file__).resolve().parent
db = root / "data" / "leadflow.db"
out = root / "backups"
out.mkdir(exist_ok=True)
if not db.exists():
    raise SystemExit(f"Database not found: {db}")
target = out / f"leadflow-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
shutil.copy2(db, target)
print(f"Backup created: {target}")
