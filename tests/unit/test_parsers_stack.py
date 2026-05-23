from app.parsers.stack import extract_stack


def test_python_django():
    out = extract_stack("Senior Python Developer with Django and PostgreSQL")
    assert "python" in out
    assert "django" in out
    assert "postgresql" in out


def test_fastapi_asyncio():
    out = extract_stack("FastAPI asyncio backend service")
    assert "fastapi" in out
    assert "asyncio" in out


def test_kubernetes_docker():
    out = extract_stack("Docker, Kubernetes, AWS")
    assert "docker" in out
    assert "kubernetes" in out
    assert "aws" in out


def test_empty_text():
    assert extract_stack("") == []


def test_no_tech_returned_empty():
    out = extract_stack("Менеджер по продажам, опыт работы 5 лет")
    assert out == []


def test_unique_results():
    out = extract_stack("Python Python python PYTHON")
    assert out.count("python") == 1


def test_clickhouse_kafka():
    out = extract_stack("Опыт с ClickHouse и Kafka обязателен")
    assert "clickhouse" in out
    assert "kafka" in out
