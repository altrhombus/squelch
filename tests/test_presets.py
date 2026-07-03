import aiosqlite

from backend.stations import (
    PresetCreate,
    PresetUpdate,
    create_preset,
    delete_preset,
    get_preset,
    list_presets,
    seed_default_presets,
    update_preset,
)


async def test_preset_crud_round_trip(tmp_db):
    await tmp_db.init_db()

    created = await create_preset(PresetCreate(name="WBEZ", frequency=91.5, band="fm"))
    assert created["id"] is not None
    assert created["gain"] == "auto"

    assert any(p["name"] == "WBEZ" for p in await list_presets())

    updated = await update_preset(created["id"], PresetUpdate(name="WBEZ 91.5"))
    assert updated["name"] == "WBEZ 91.5"
    assert updated["frequency"] == 91.5  # untouched fields preserved

    # Empty update is a no-op that returns the current row
    same = await update_preset(created["id"], PresetUpdate())
    assert same == updated

    assert await delete_preset(created["id"]) is True
    assert await delete_preset(created["id"]) is False
    assert await get_preset(created["id"]) is None


async def test_seed_defaults_only_into_empty_table(tmp_db):
    await tmp_db.init_db()
    defaults = [{"name": "Example FM", "frequency": 91.1, "band": "fm"}]

    await seed_default_presets(defaults)
    assert len(await list_presets()) == 1

    # Second seeding (e.g. every service restart) must not duplicate
    await seed_default_presets(defaults)
    assert len(await list_presets()) == 1


async def test_wx_band_migration_preserves_rows(tmp_db):
    # Simulate a database created before the 'wx' band existed
    async with aiosqlite.connect(tmp_db.DB_PATH) as db:
        await db.execute("""
            CREATE TABLE presets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                frequency REAL NOT NULL,
                band TEXT NOT NULL CHECK(band IN ('fm', 'am', 'scanner', 'hd')),
                gain TEXT DEFAULT 'auto',
                bandwidth TEXT DEFAULT 'wide',
                stereo_mode TEXT DEFAULT 'auto',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute(
            "INSERT INTO presets (name, frequency, band) VALUES ('Old', 91.1, 'fm')"
        )
        await db.commit()

    await tmp_db.init_db()  # triggers the wx migration

    presets = await list_presets()
    assert [p["name"] for p in presets] == ["Old"]

    # And the new constraint accepts 'wx'
    wx = await create_preset(PresetCreate(name="NOAA", frequency=162.55, band="wx"))
    assert wx["band"] == "wx"
