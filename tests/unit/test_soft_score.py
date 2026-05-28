"""Тесты employer_soft_score — числовой soft-skills score работодателя."""

from __future__ import annotations

from app.scoring.match import employer_soft_score


def test_none_when_no_data():
    assert employer_soft_score(None) is None
    assert employer_soft_score({}) is None
    assert employer_soft_score({"team_culture": "modern"}) is None  # нет числовых/тона


def test_full_data():
    # wlb=8, growth=6, tone=warm(10): (8*0.4 + 6*0.3 + 10*0.3)/1.0 = 3.2+1.8+3.0 = 8.0 → 80
    assert employer_soft_score({"wlb_score": 8, "growth_opportunities": 6, "tone": "warm"}) == 80


def test_aggressive_tone_low():
    s = employer_soft_score({"wlb_score": 2, "growth_opportunities": 2, "tone": "aggressive"})
    assert s is not None and s < 30


def test_partial_components_reweighted():
    # только tone=neutral(6) → 60 (вес нормируется)
    assert employer_soft_score({"tone": "neutral"}) == 60
    # только wlb=10 → 100
    assert employer_soft_score({"wlb_score": 10}) == 100


def test_clamp():
    assert 0 <= employer_soft_score({"wlb_score": 10, "growth_opportunities": 10, "tone": "warm"}) <= 100
