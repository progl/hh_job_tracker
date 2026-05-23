from app.parsers.remote import is_hybrid_by_text, is_remote_by_text


def test_remote_keyword_ru():
    assert is_remote_by_text("полностью удалённая работа") is True


def test_remote_keyword_en():
    assert is_remote_by_text("Remote first company") is True


def test_remote_keyword_distant():
    assert is_remote_by_text("работа дистанционно из дома") is True


def test_remote_false_on_office():
    assert is_remote_by_text("офис в центре Москвы") is False


def test_remote_empty():
    assert is_remote_by_text("") is False


def test_hybrid_detected():
    assert is_hybrid_by_text("гибридный график 3/2") is True


def test_hybrid_not_remote():
    assert is_hybrid_by_text("полностью удалённо") is False
