from zoneinfo import ZoneInfo
from datetime import datetime

BRT = ZoneInfo("America/Sao_Paulo")

def to_brt(dt: datetime | None):
    if not dt:
        return None
    if dt.tzinfo is None:
        # segurança extra (caso venha naive)
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(BRT)
