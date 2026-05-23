from typing import Any


def _norm_skills(items: list) -> set[str]:
    out = set()
    for s in items or []:
        if not s:
            continue
        out.add(str(s).strip().lower())
    return out


SCALES: dict[str, str] = {
    "стек": "35 если все скиллы профиля совпали с распознанным стеком вакансии; ratio = |intersection| / |union|, score = ratio×35",
    "ЗП": "salary_rub / salary_expected_from: ≥100% → 20, ≥85% → 15, ≥70% → 10, ≥50% → 5, иначе 0. Если ЗП не указана — 4. Если ожидания не заданы — 12",
    "формат": "15 если удалёнка (schedule=remote или эвристика по тексту), 0 иначе",
    "опыт": "сопоставление detected level (junior/middle/senior/lead) и years_experience из профиля. Сеньор+4 года → 15; мидл 2-6 лет → 13; и т.д.",
    "вежливость": "комбо read_topic_percent (≥90→7, ≥70→5, ≥50→3, ≥30→1) + reply_working_days (≤3→+3, ≤7→+1). Cap 10. Если нет данных — 4",
    "конкуренция": "по total_responses_count: <30 → 5, <100 → 4, <250 → 2, <500 → 1, ≥500 → 0. Меньше откликов = выше шанс заметить",
}


def score_vacancy(
    v: dict[str, Any],
    profile: dict[str, Any] | None,
    employer_pol: dict[str, Any] | None = None,
) -> dict[str, Any]:
    parts: list[dict[str, Any]] = []
    score = 0
    profile = profile or {}

    user_skills = _norm_skills(profile.get("skills") or [])
    parsed = _norm_skills(v.get("parsed_stack") or [])

    # 1) Стек, 35 баллов
    if user_skills and parsed:
        overlap = user_skills & parsed
        ratio = len(overlap) / max(len(user_skills | parsed), 1)
        s = int(ratio * 35)
        parts.append({"f": "стек", "v": s, "max": 35, "note": f"{len(overlap)} совпадений из {len(user_skills | parsed)}", "ok": s >= 17})
    elif parsed and not user_skills:
        s = 20
        parts.append({"f": "стек", "v": s, "max": 35, "note": "профиль не заполнен", "ok": True})
    else:
        s = 0
        parts.append({"f": "стек", "v": 0, "max": 35, "note": "нет данных", "ok": False})
    score += s

    # 2) ЗП, 20 баллов
    expected = profile.get("salary_expected_from")
    sal = v.get("salary_rub")
    if expected and sal:
        ratio = sal / expected
        if ratio >= 1.0:
            s = 20
        elif ratio >= 0.85:
            s = 15
        elif ratio >= 0.7:
            s = 10
        elif ratio >= 0.5:
            s = 5
        else:
            s = 0
        parts.append({"f": "ЗП", "v": s, "max": 20, "note": f"{sal:,} vs ожидание {expected:,}".replace(',', ' '), "ok": s >= 10})
    elif sal:
        s = 12
        parts.append({"f": "ЗП", "v": s, "max": 20, "note": "ожидание не задано", "ok": True})
    else:
        s = 4
        parts.append({"f": "ЗП", "v": s, "max": 20, "note": "не указана", "ok": False})
    score += s

    # 3) Формат, 15 баллов
    if v.get("is_remote") or v.get("is_remote_text"):
        s = 15
        parts.append({"f": "формат", "v": 15, "max": 15, "note": "удалёнка", "ok": True})
    else:
        s = 0
        parts.append({"f": "формат", "v": 0, "max": 15, "note": "офис/гибрид", "ok": False})
    score += s

    # 4) Уровень/опыт, 15 баллов
    years = profile.get("years_experience") or 0
    level = (v.get("level") or "").lower()
    s = 0
    note = f"{level or '—'} vs {years} лет"
    if level == "senior":
        s = 15 if years >= 4 else 8 if years >= 3 else 4
    elif level == "lead":
        s = 15 if years >= 5 else 8 if years >= 4 else 3
    elif level == "middle":
        s = 13 if 2 <= years <= 6 else 8
    elif level == "junior":
        s = 6 if years <= 2 else 8
    elif level == "intern":
        s = 2
    else:
        s = 8
        note = "уровень не определён"
    parts.append({"f": "опыт", "v": s, "max": 15, "note": note, "ok": s >= 10})
    score += s

    # 5) Вежливость работодателя, 10 баллов
    if employer_pol:
        rtp = employer_pol.get("read_topic_percent") or 0
        rwd = employer_pol.get("reply_working_days")
        s = 0
        if rtp >= 90:
            s = 7
        elif rtp >= 70:
            s = 5
        elif rtp >= 50:
            s = 3
        elif rtp >= 30:
            s = 1
        if rwd is not None and rwd <= 3:
            s += 3
        elif rwd is not None and rwd <= 7:
            s += 1
        s = min(s, 10)
        parts.append({"f": "вежливость", "v": s, "max": 10, "note": f"читает {rtp}%, ответ за {rwd}д", "ok": s >= 6})
    else:
        s = 4
        parts.append({"f": "вежливость", "v": s, "max": 10, "note": "нет данных", "ok": False})
    score += s

    # 6) Конкуренция, 5 баллов
    total = v.get("total_responses_count") or 0
    if total < 30:
        s = 5
    elif total < 100:
        s = 4
    elif total < 250:
        s = 2
    elif total < 500:
        s = 1
    else:
        s = 0
    parts.append({"f": "конкуренция", "v": s, "max": 5, "note": f"откликов {total}", "ok": s >= 3})
    score += s

    for p in parts:
        p["scale"] = SCALES.get(p["f"], "")
    return {"score": score, "max": 100, "parts": parts}
