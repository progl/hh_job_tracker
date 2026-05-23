from app.parsers.level import detect_level


def test_senior_detected_by_keyword():
    assert detect_level("Senior Python Developer") == "senior"


def test_lead_by_team_lead():
    assert detect_level("Tech Lead / Team Lead Python") == "lead"


def test_lead_by_russian_keyword():
    assert detect_level("руководитель команды разработки") == "lead"


def test_lead_by_engineering_manager():
    assert detect_level("Engineering Manager") == "lead"


def test_middle():
    assert detect_level("Middle Python Developer") == "middle"


def test_junior():
    assert detect_level("Junior Python") == "junior"


def test_intern():
    assert detect_level("Стажёр / Intern в команду") == "intern"


def test_empty_returns_none():
    assert detect_level("") is None


def test_unrelated_returns_none():
    assert detect_level("Backend Engineer") is None
