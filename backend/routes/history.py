"""Recently-heard history."""

from fastapi import APIRouter

from ..history import clear_history, delete_history_item, list_history

router = APIRouter()


@router.get("/history")
async def get_history():
    return await list_history(limit=100)


@router.delete("/history/{history_id}", status_code=204)
async def remove_history_item(history_id: int):
    await delete_history_item(history_id)


@router.delete("/history", status_code=204)
async def remove_all_history():
    await clear_history()
