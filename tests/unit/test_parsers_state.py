from app.parsers.state import extract_initial_state


def test_extracts_initial_state():
    html = '''
    <html><body>
      <template id="HH-Lux-InitialState">{"foo": 1, "bar": "test"}</template>
    </body></html>
    '''
    out = extract_initial_state(html)
    assert out == {"foo": 1, "bar": "test"}


def test_returns_none_if_no_template():
    assert extract_initial_state("<html></html>") is None


def test_unescapes_html_entities():
    html = '<template id="HH-Lux-InitialState">{"name": "Acme &amp; Co"}</template>'
    out = extract_initial_state(html)
    assert out == {"name": "Acme & Co"}


def test_nested_structure():
    html = '<template id="HH-Lux-InitialState">{"a": {"b": [1, 2, 3]}}</template>'
    out = extract_initial_state(html)
    assert out["a"]["b"] == [1, 2, 3]
