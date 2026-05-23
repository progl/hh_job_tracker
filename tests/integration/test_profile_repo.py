import pytest

from app.db import profile_repo


@pytest.mark.asyncio
async def test_get_profile_returns_none_initially(tmp_db):
    assert await profile_repo.get_profile(tmp_db) is None


@pytest.mark.asyncio
async def test_upsert_from_state_creates_profile(tmp_db):
    state = {"hhid": "12345", "account": {"firstName": "Иван", "lastName": "Петров"}}
    await profile_repo.upsert_from_state(tmp_db, state)
    p = await profile_repo.get_profile(tmp_db)
    assert p["hhid"] == "12345"
    assert p["full_name"] == "Иван Петров"


@pytest.mark.asyncio
async def test_update_manual_only_allowed_fields(tmp_db):
    await profile_repo.upsert_from_state(tmp_db, {"hhid": "1", "account": {"firstName": "X"}})
    await profile_repo.update_manual(tmp_db, {"title": "Senior", "years_experience": 5, "evil_field": "boom"})
    p = await profile_repo.get_profile(tmp_db)
    assert p["title"] == "Senior"
    assert p["years_experience"] == 5
    assert "evil_field" not in p


@pytest.mark.asyncio
async def test_update_manual_skills_list(tmp_db):
    await profile_repo.upsert_from_state(tmp_db, {"hhid": "1", "account": {"firstName": "X"}})
    await profile_repo.update_manual(tmp_db, {"skills": ["python", "django"]})
    p = await profile_repo.get_profile(tmp_db)
    assert p["skills"] == ["python", "django"]


@pytest.mark.asyncio
async def test_set_from_resume_extracts_fields(tmp_db):
    resume = {
        "_attributes": {"id": "res-1"},
        "title": [{"string": "Senior Python Developer"}],
        "keySkills": [{"string": "python"}, {"string": "django"}],
        "salary": [{"amount": 250000, "currency": "RUR"}],
        "totalExperience": [{"amount": 60}],  # 60 месяцев = 5 лет
        "workFormats": [{"string": "REMOTE"}],
    }
    info = await profile_repo.set_from_resume(tmp_db, resume)
    assert info["resume_id"] == "res-1"
    assert info["years_experience"] == 5.0
    assert info["skills_count"] == 2
    p = await profile_repo.get_profile(tmp_db)
    assert p["resume_id"] == "res-1"
    assert "python" in p["skills"]
    assert p["title"] == "Senior Python Developer"
