from app.scoring.match import score_vacancy

PROFILE_FULL = {
    "skills": ["python", "django", "postgresql"],
    "years_experience": 5,
    "salary_expected_from": 200000,
}


def test_full_stack_match():
    v = {
        "parsed_stack": ["python", "django", "postgresql"],
        "salary_rub": 250000,
        "is_remote": 1,
        "level": "senior",
    }
    r = score_vacancy(v, PROFILE_FULL, {"read_topic_percent": 90, "reply_working_days": 2.0})
    assert r["score"] >= 80
    assert r["max"] == 100
    assert {p["f"] for p in r["parts"]} == {"стек", "ЗП", "формат", "опыт", "вежливость", "конкуренция"}


def test_no_remote_zero():
    v = {"parsed_stack": ["python"], "salary_rub": 200000, "is_remote": 0, "level": "middle"}
    r = score_vacancy(v, PROFILE_FULL, None)
    fmt_part = next(p for p in r["parts"] if p["f"] == "формат")
    assert fmt_part["v"] == 0


def test_salary_below_expected_zero():
    v = {"parsed_stack": ["python"], "salary_rub": 50000, "is_remote": 1, "level": "senior"}
    r = score_vacancy(v, PROFILE_FULL, None)
    zp = next(p for p in r["parts"] if p["f"] == "ЗП")
    assert zp["v"] == 0


def test_empty_profile():
    v = {"parsed_stack": ["python"], "salary_rub": 200000, "is_remote": 1, "level": "senior"}
    r = score_vacancy(v, None, None)
    assert 0 < r["score"] <= 100


def test_empty_vacancy():
    r = score_vacancy({}, PROFILE_FULL, None)
    assert r["score"] >= 0
    assert r["max"] == 100


def test_competition_penalty():
    v_low = {"parsed_stack": ["python"], "total_responses_count": 5}
    v_high = {"parsed_stack": ["python"], "total_responses_count": 1000}
    s_low = next(p for p in score_vacancy(v_low, PROFILE_FULL, None)["parts"] if p["f"] == "конкуренция")
    s_high = next(p for p in score_vacancy(v_high, PROFILE_FULL, None)["parts"] if p["f"] == "конкуренция")
    assert s_low["v"] > s_high["v"]


def test_politeness_boosts():
    v = {"parsed_stack": ["python"], "salary_rub": 200000, "is_remote": 1, "level": "senior"}
    good = score_vacancy(v, PROFILE_FULL, {"read_topic_percent": 95, "reply_working_days": 1.0})
    poor = score_vacancy(v, PROFILE_FULL, {"read_topic_percent": 20, "reply_working_days": 10.0})
    g = next(p for p in good["parts"] if p["f"] == "вежливость")
    p = next(p for p in poor["parts"] if p["f"] == "вежливость")
    assert g["v"] > p["v"]
