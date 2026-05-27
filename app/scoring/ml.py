"""ML-предиктор шанса получить приглашение.

Обучение: на datasets, экспортированных из текущих негоций (см. scripts/export_dataset.py).
Использует логистическую регрессию sklearn. Сохраняется в data/model.pkl.

Если положительных примеров < MIN_POSITIVES — пропускаем обучение, используется эвристика.
"""

import logging
from pathlib import Path
from typing import Any

import joblib

from app.db import employers_repo, vacancies_repo
from app.db.db import get_db

log = logging.getLogger(__name__)

MODEL_PATH = Path("data/model.pkl")
MIN_POSITIVES = 5
MIN_NEGATIVES = 5

FEATURES = [
    "viewed_by_opponent",
    "has_response_letter",
    "conversation_messages",
    "emp_read_pct",
    "emp_reply_days",
    "emp_all_topics",
    "salary_rub",
    "is_remote",
    "stack_count",
    "total_responses",
]


async def _build_dataset(db) -> tuple[list[list[float]], list[int], dict[str, Any]]:
    emp_map = await employers_repo.get_map(db)
    cur = await db.execute(
        """
        SELECT n.id, n.vacancy_id, n.employer_id, n.last_state, n.viewed_by_opponent,
               n.conversation_messages, n.has_response_letter
          FROM negotiations n
         WHERE n.last_state IN ('INVITATION','INTERVIEW') OR n.last_state LIKE 'DISCARD%'
        """
    )
    rows = await cur.fetchall()
    X: list[list[float]] = []
    y: list[int] = []
    for r in rows:
        emp = emp_map.get(r["employer_id"]) if r["employer_id"] else None
        v = await vacancies_repo.get_vacancy(db, r["vacancy_id"]) if r["vacancy_id"] else None
        feat = {
            "viewed_by_opponent": float(r["viewed_by_opponent"] or 0),
            "has_response_letter": float(r["has_response_letter"] or 0),
            "conversation_messages": float(r["conversation_messages"] or 0),
            "emp_read_pct": float((emp or {}).get("read_topic_percent") or 50),
            "emp_reply_days": float((emp or {}).get("reply_working_days") or 7),
            "emp_all_topics": float((emp or {}).get("all_topic_count") or 0),
            "salary_rub": float((v or {}).get("salary_rub") or 0),
            "is_remote": float(bool((v or {}).get("is_remote") or (v or {}).get("is_remote_text"))),
            "stack_count": float(len((v or {}).get("parsed_stack") or [])),
            "total_responses": float((v or {}).get("total_responses_count") or 0),
        }
        X.append([feat[k] for k in FEATURES])
        y.append(1 if r["last_state"] in ("INVITATION", "INTERVIEW") else 0)
    return X, y, {"rows": len(X), "positives": sum(y), "negatives": len(y) - sum(y)}


async def train_if_enough_data() -> dict[str, Any]:
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import StratifiedKFold, cross_val_score
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    db = await get_db()
    try:
        X, y, stats = await _build_dataset(db)
    finally:
        await db.close()
    if stats["positives"] < MIN_POSITIVES or stats["negatives"] < MIN_NEGATIVES:
        log.info("ml: not enough data (%s) — skip training", stats)
        return {"trained": False, **stats}
    scaler = StandardScaler()
    Xs = scaler.fit_transform(X)
    clf = LogisticRegression(max_iter=2000, class_weight="balanced")
    clf.fit(Xs, y)
    try:
        train_auc = float(roc_auc_score(y, clf.predict_proba(Xs)[:, 1]))
    except ValueError:
        train_auc = None

    cv_auc_mean: float | None = None
    cv_auc_std: float | None = None
    cv_scores: list[float] | None = None
    n_splits = min(5, stats["positives"], stats["negatives"])
    if n_splits >= 2:
        try:
            pipe = Pipeline(
                [
                    ("scaler", StandardScaler()),
                    ("clf", LogisticRegression(max_iter=2000, class_weight="balanced")),
                ]
            )
            skf = StratifiedKFold(n_splits=n_splits, shuffle=True, random_state=42)
            scores = cross_val_score(pipe, X, y, cv=skf, scoring="roc_auc")
            cv_scores = [float(s) for s in scores]
            cv_auc_mean = float(np.mean(scores))
            cv_auc_std = float(np.std(scores))
            log.debug("ml: cv folds=%s scores=%s", n_splits, [round(s, 3) for s in cv_scores])
        except Exception as e:
            log.debug("ml: cv failed: %s", e)
    else:
        log.debug("ml: cv skipped, n_splits=%s (need >=2)", n_splits)

    try:
        coefs = dict(zip(FEATURES, (float(c) for c in clf.coef_[0]), strict=False))
        top = sorted(coefs.items(), key=lambda kv: abs(kv[1]), reverse=True)
        log.debug("ml: feature weights (sorted by |w|): %s", [(k, round(v, 3)) for k, v in top])
    except Exception as e:
        log.debug("ml: weights dump failed: %s", e)

    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump({"scaler": scaler, "clf": clf, "features": FEATURES}, MODEL_PATH)
    log.info(
        "ml: trained n=%s (pos=%s, neg=%s) train_auc=%s cv_auc=%s±%s (k=%s)",
        stats["rows"],
        stats["positives"],
        stats["negatives"],
        round(train_auc, 3) if train_auc is not None else None,
        round(cv_auc_mean, 3) if cv_auc_mean is not None else None,
        round(cv_auc_std, 3) if cv_auc_std is not None else None,
        n_splits if n_splits >= 2 else 0,
    )
    return {
        "trained": True,
        "auc": train_auc,
        "cv_auc_mean": cv_auc_mean,
        "cv_auc_std": cv_auc_std,
        "cv_scores": cv_scores,
        "cv_splits": n_splits if n_splits >= 2 else 0,
        "model_path": str(MODEL_PATH),
        **stats,
    }


_MODEL = None


def _load() -> dict | None:
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    if not MODEL_PATH.exists():
        return None
    try:
        _MODEL = joblib.load(MODEL_PATH)
        return _MODEL
    except Exception as e:
        log.warning("ml: load failed: %s", e)
        return None


def predict_ml(features: dict[str, float]) -> float | None:
    m = _load()
    if not m:
        return None
    vec = [[features.get(k, 0.0) for k in m["features"]]]
    try:
        vec_s = m["scaler"].transform(vec)
        return float(m["clf"].predict_proba(vec_s)[0, 1])
    except Exception as e:
        log.warning("ml: predict failed: %s", e)
        return None


def reload_model() -> None:
    global _MODEL
    _MODEL = None
    _load()
