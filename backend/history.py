"""History persistence — recently heard tracks (written by MetadataState)."""

from .db import get_db


async def list_history(limit: int = 100) -> list[dict]:
    db = await get_db()
    async with db.execute(
        "SELECT * FROM history ORDER BY seen_at DESC LIMIT ?", (limit,)
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def delete_history_item(history_id: int):
    db = await get_db()
    await db.execute("DELETE FROM history WHERE id = ?", (history_id,))
    await db.commit()


async def clear_history():
    db = await get_db()
    await db.execute("DELETE FROM history")
    await db.commit()
