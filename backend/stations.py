from typing import Optional
from pydantic import BaseModel
from .db import get_db


class PresetCreate(BaseModel):
    name: str
    frequency: float
    band: str
    gain: str = "auto"
    stereo_mode: str = "auto"


class PresetUpdate(BaseModel):
    name: Optional[str] = None
    frequency: Optional[float] = None
    band: Optional[str] = None
    gain: Optional[str] = None
    stereo_mode: Optional[str] = None


async def list_presets() -> list[dict]:
    db = await get_db()
    try:
        async with db.execute(
            "SELECT * FROM presets ORDER BY band, frequency"
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]
    finally:
        await db.close()


async def get_preset(preset_id: int) -> Optional[dict]:
    db = await get_db()
    try:
        async with db.execute(
            "SELECT * FROM presets WHERE id = ?", (preset_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None
    finally:
        await db.close()


async def create_preset(p: PresetCreate) -> dict:
    db = await get_db()
    try:
        cur = await db.execute(
            """INSERT INTO presets (name, frequency, band, gain, stereo_mode)
               VALUES (?, ?, ?, ?, ?)""",
            (p.name, p.frequency, p.band, p.gain, p.stereo_mode),
        )
        await db.commit()
        return await get_preset(cur.lastrowid)
    finally:
        await db.close()


async def update_preset(preset_id: int, p: PresetUpdate) -> Optional[dict]:
    fields = {k: v for k, v in p.model_dump().items() if v is not None}
    if not fields:
        return await get_preset(preset_id)
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    db = await get_db()
    try:
        await db.execute(
            f"UPDATE presets SET {set_clause} WHERE id = ?",
            (*fields.values(), preset_id),
        )
        await db.commit()
        return await get_preset(preset_id)
    finally:
        await db.close()


async def delete_preset(preset_id: int) -> bool:
    db = await get_db()
    try:
        cur = await db.execute("DELETE FROM presets WHERE id = ?", (preset_id,))
        await db.commit()
        return cur.rowcount > 0
    finally:
        await db.close()


async def seed_default_presets(defaults: list[dict]):
    db = await get_db()
    try:
        async with db.execute("SELECT COUNT(*) FROM presets") as cur:
            row = await cur.fetchone()
            if row[0] > 0:
                return
        for p in defaults:
            await db.execute(
                "INSERT INTO presets (name, frequency, band) VALUES (?, ?, ?)",
                (p["name"], p["frequency"], p["band"]),
            )
        await db.commit()
    finally:
        await db.close()
