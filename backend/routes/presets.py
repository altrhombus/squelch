"""Preset CRUD."""

from fastapi import APIRouter, HTTPException

from ..stations import (
    PresetCreate,
    PresetUpdate,
    create_preset,
    delete_preset,
    list_presets,
    update_preset,
)

router = APIRouter()


@router.get("/presets")
async def get_presets():
    return await list_presets()


@router.post("/presets", status_code=201)
async def post_preset(p: PresetCreate):
    return await create_preset(p)


@router.patch("/presets/{preset_id}")
async def patch_preset(preset_id: int, p: PresetUpdate):
    result = await update_preset(preset_id, p)
    if not result:
        raise HTTPException(404, "Preset not found")
    return result


@router.delete("/presets/{preset_id}", status_code=204)
async def remove_preset(preset_id: int):
    if not await delete_preset(preset_id):
        raise HTTPException(404, "Preset not found")
