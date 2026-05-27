from app.scoring.predict import predict_invite_prob


def test_returns_dict_with_prob():
    r = predict_invite_prob(50, None, 100, vacancy={"salary_rub": 150000})
    assert "prob" in r
    assert "source" in r
    assert 1 <= r["prob"] <= 95


def test_higher_score_higher_prob():
    a = predict_invite_prob(30, None, 100, vacancy={})
    b = predict_invite_prob(80, None, 100, vacancy={})
    assert b["prob"] > a["prob"]


def test_good_politeness_boosts():
    base = predict_invite_prob(50, None, 100, vacancy={})
    boosted = predict_invite_prob(50, {"read_topic_percent": 95, "reply_working_days": 1.0}, 100, vacancy={})
    assert boosted["prob"] >= base["prob"]


def test_high_competition_penalizes():
    low = predict_invite_prob(60, None, 50, vacancy={})
    high = predict_invite_prob(60, None, 1000, vacancy={})
    assert low["prob"] > high["prob"]


def test_no_vacancy_uses_heuristic():
    r = predict_invite_prob(50, None, 100, vacancy=None)
    assert r["source"] == "heuristic"


def test_explain_present_for_heuristic():
    r = predict_invite_prob(50, None, 100, vacancy={})
    assert "explain" in r


def test_prob_capped():
    r_min = predict_invite_prob(0, {"read_topic_percent": 0}, 5000, vacancy={})
    r_max = predict_invite_prob(100, {"read_topic_percent": 100, "reply_working_days": 0.1}, 0, vacancy={})
    assert r_min["prob"] >= 1
    assert r_max["prob"] <= 95
