import pickle
from pathlib import Path

import joblib
import pytest

from app.scoring import ml


@pytest.fixture(autouse=True)
def _reset_model():
    ml._MODEL = None
    yield
    ml._MODEL = None


def test_predict_returns_none_without_model(monkeypatch, tmp_path):
    monkeypatch.setattr(ml, "MODEL_PATH", tmp_path / "nope.pkl")
    assert ml.predict_ml({"viewed_by_opponent": 1}) is None


def test_load_returns_cached(monkeypatch):
    ml._MODEL = {"features": ["a"], "clf": "x", "scaler": "y"}
    res = ml._load()
    assert res is ml._MODEL


def test_load_handles_corrupt_file(monkeypatch, tmp_path):
    bad = tmp_path / "broken.pkl"
    bad.write_bytes(b"garbage")
    monkeypatch.setattr(ml, "MODEL_PATH", bad)
    assert ml._load() is None


def test_predict_ml_with_trained_model(monkeypatch, tmp_path):
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    import numpy as np

    # обучим минимальную модель
    X = [[0, 0], [1, 1], [0, 1], [1, 0], [0, 0], [1, 1]]
    y = [0, 1, 0, 1, 0, 1]
    scaler = StandardScaler()
    Xs = scaler.fit_transform(X)
    clf = LogisticRegression().fit(Xs, y)
    path = tmp_path / "m.pkl"
    joblib.dump({"scaler": scaler, "clf": clf, "features": ["a", "b"]}, path)
    monkeypatch.setattr(ml, "MODEL_PATH", path)

    p = ml.predict_ml({"a": 1, "b": 1})
    assert p is not None
    assert 0.0 <= p <= 1.0


def test_predict_ml_handles_predict_failure(monkeypatch):
    class BadClf:
        def predict_proba(self, X):
            raise RuntimeError("nope")

    class IdentScaler:
        def transform(self, X):
            return X

    # минуем _load(): подсовываем готовую модель в кэш _MODEL
    monkeypatch.setattr(ml, "_MODEL", {"scaler": IdentScaler(), "clf": BadClf(), "features": ["a"]})
    assert ml.predict_ml({"a": 1.0}) is None


def test_reload_model_clears_and_reloads(monkeypatch, tmp_path):
    monkeypatch.setattr(ml, "MODEL_PATH", tmp_path / "no.pkl")
    ml._MODEL = {"features": ["x"]}
    ml.reload_model()
    # _MODEL = None после reload + _load() не нашёл файла
    assert ml._MODEL is None


@pytest.mark.asyncio
async def test_train_if_enough_data_skips_when_no_data(tmp_db, monkeypatch):
    """Без данных — обучение пропускается."""
    # дублируем _resync_get_db паттерн для scoring/ml.py
    import app.db.db as dbm
    monkeypatch.setattr(ml, "get_db", dbm.get_db)
    # employers/vacancies — тоже могут быть перезагружены
    import app.db.employers_repo as er
    import app.db.vacancies_repo as vr
    monkeypatch.setattr(ml, "employers_repo", er)
    monkeypatch.setattr(ml, "vacancies_repo", vr)

    res = await ml.train_if_enough_data()
    assert res["trained"] is False
    assert res["rows"] == 0


@pytest.mark.asyncio
async def test_build_dataset_collects_features(tmp_db, monkeypatch):
    import app.db.db as dbm
    import app.db.employers_repo as er
    import app.db.vacancies_repo as vr
    monkeypatch.setattr(ml, "get_db", dbm.get_db)
    monkeypatch.setattr(ml, "employers_repo", er)
    monkeypatch.setattr(ml, "vacancies_repo", vr)

    # 2 negotiations: одна INVITATION (positive), одна DISCARD (negative)
    await tmp_db.execute(
        "INSERT INTO negotiations(id, vacancy_id, employer_id, last_state, viewed_by_opponent, "
        "conversation_messages, has_response_letter) VALUES (1, 100, 10, 'INVITATION', 1, 2, 1)"
    )
    await tmp_db.execute(
        "INSERT INTO negotiations(id, vacancy_id, employer_id, last_state, viewed_by_opponent, "
        "conversation_messages, has_response_letter) VALUES (2, 200, 10, 'DISCARD_BY_X', 0, 0, 0)"
    )
    # employer
    await tmp_db.execute(
        "INSERT INTO employers(id, read_topic_percent, reply_working_days, all_topic_count) "
        "VALUES (10, 70, 3.0, 50)"
    )
    # vacancies
    await tmp_db.execute(
        "INSERT INTO vacancies(id, name, salary_rub, is_remote, parsed_stack, total_responses_count) "
        "VALUES (100, 'A', 200000, 1, '[\"python\",\"django\"]', 10)"
    )
    await tmp_db.execute(
        "INSERT INTO vacancies(id, name, salary_rub) VALUES (200, 'B', 50000)"
    )
    await tmp_db.commit()

    X, y, stats = await ml._build_dataset(tmp_db)
    assert stats["rows"] == 2
    assert stats["positives"] == 1
    assert stats["negatives"] == 1
    assert len(X) == 2
    # первый ряд — положительный
    assert y[0] == 1
    # эти 10 признаков
    assert len(X[0]) == len(ml.FEATURES)


@pytest.mark.asyncio
async def test_train_if_enough_data_trains_with_enough(tmp_db, monkeypatch, tmp_path):
    import app.db.db as dbm
    import app.db.employers_repo as er
    import app.db.vacancies_repo as vr
    monkeypatch.setattr(ml, "get_db", dbm.get_db)
    monkeypatch.setattr(ml, "employers_repo", er)
    monkeypatch.setattr(ml, "vacancies_repo", vr)
    monkeypatch.setattr(ml, "MODEL_PATH", tmp_path / "m.pkl")

    # 10 positives + 10 negatives
    for i in range(10):
        await tmp_db.execute(
            "INSERT INTO negotiations(id, vacancy_id, last_state, viewed_by_opponent, "
            "conversation_messages, has_response_letter) VALUES (?, ?, 'INVITATION', 1, 2, 1)",
            (i + 1, 1000 + i),
        )
    for i in range(10):
        await tmp_db.execute(
            "INSERT INTO negotiations(id, vacancy_id, last_state, viewed_by_opponent) "
            "VALUES (?, ?, 'DISCARD_BY_EMPLOYER', 0)",
            (i + 100, 2000 + i),
        )
    await tmp_db.commit()

    res = await ml.train_if_enough_data()
    assert res["trained"] is True
    assert res["positives"] == 10
    assert res["negatives"] == 10
    assert (tmp_path / "m.pkl").exists()
