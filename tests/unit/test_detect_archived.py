from app.collector.vacancies import _detect_archived


def test_archived_html_marker_classic():
    assert _detect_archived(None, "Вакансия в архиве") is True


def test_archived_html_marker_with_date():
    assert _detect_archived(None, "<div>В архиве с 22 мая 2026</div>") is True


def test_archived_html_marker_class():
    assert _detect_archived(None, '<div class="vacancy-archived">stub</div>') is True


def test_html_active_not_archived():
    assert _detect_archived(None, "<div>Активная вакансия, отклик принимается</div>") is False


def test_state_at_archived_true():
    assert _detect_archived({"@archived": True}, "") is True


def test_state_archived_string_true():
    assert _detect_archived({"archived": "true"}, "") is True


def test_state_is_archived_alias():
    assert _detect_archived({"isArchived": True}, "") is True


def test_state_status_archived():
    assert _detect_archived({"status": "archived"}, "") is True


def test_state_status_active():
    assert _detect_archived({"status": "active"}, "") is False


def test_state_archived_false():
    assert _detect_archived({"archived": False, "status": "active"}, "") is False


def test_both_none():
    assert _detect_archived(None, None) is False


def test_state_takes_precedence_active_html():
    # Если в state явный @archived=True — побеждает (мы не разбираем что в HTML)
    assert _detect_archived({"@archived": True}, "<div>Активная</div>") is True
