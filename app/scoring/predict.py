"""Предиктор шанса получить приглашение.

Стратегия:
1. Если обученная ML-модель есть (data/model.pkl) — используем её.
2. Иначе — эвристика на основе match-score, индекса вежливости работодателя и конкуренции.
"""
from typing import Any

from app.scoring import ml as ml_module


def predict_invite_prob(
    score: int,
    employer_pol: dict[str, Any] | None,
    total_responses: int | None,
    vacancy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # 1) ML
    if vacancy is not None:
        feats = {
            "viewed_by_opponent": 0.0,
            "has_response_letter": 0.0,
            "conversation_messages": 0.0,
            "emp_read_pct": float((employer_pol or {}).get("read_topic_percent") or 50),
            "emp_reply_days": float((employer_pol or {}).get("reply_working_days") or 7),
            "emp_all_topics": float((employer_pol or {}).get("all_topic_count") or 0),
            "salary_rub": float(vacancy.get("salary_rub") or 0),
            "is_remote": float(bool(vacancy.get("is_remote") or vacancy.get("is_remote_text"))),
            "stack_count": float(len(vacancy.get("parsed_stack") or [])),
            "total_responses": float(vacancy.get("total_responses_count") or total_responses or 0),
        }
        ml_p = ml_module.predict_ml(feats)
        if ml_p is not None:
            return {"prob": round(ml_p * 100), "source": "ml"}

    # 2) Эвристика
    base = score / 100.0
    rtp = (employer_pol or {}).get("read_topic_percent")
    rwd = (employer_pol or {}).get("reply_working_days")
    pol_boost = 0.0
    if isinstance(rtp, (int, float)):
        pol_boost = (rtp - 50) / 100.0
    if isinstance(rwd, (int, float)) and rwd <= 3:
        pol_boost += 0.05
    comp_pen = 0.0
    if total_responses:
        if total_responses > 500:
            comp_pen = -0.10
        elif total_responses > 200:
            comp_pen = -0.05
    p = max(0.01, min(0.95, base * 0.6 + pol_boost * 0.3 + 0.2 + comp_pen))
    return {
        "prob": round(p * 100),
        "source": "heuristic",
        "explain": {
            "base_from_score": round(base * 60),
            "politeness_factor": round(pol_boost * 30),
            "competition_factor": round(comp_pen * 100),
        },
    }
