import re

TECH_KEYWORDS: dict[str, list[str]] = {
    "python": [r"\bpython\b"],
    "django": [r"\bdjango\b"],
    "fastapi": [r"\bfast[\s-]?api\b"],
    "flask": [r"\bflask\b"],
    "drf": [r"\bdjango[\s-]?rest[\s-]?framework\b", r"\bdrf\b"],
    "celery": [r"\bcelery\b"],
    "sqlalchemy": [r"\bsqlalchemy\b"],
    "pydantic": [r"\bpydantic\b"],
    "asyncio": [r"\basyncio\b", r"\basync[\s/]?await\b"],
    "aiohttp": [r"\baiohttp\b"],
    "graphql": [r"\bgraphql\b"],
    "rest": [r"\brest(ful)?\s*api\b"],
    "postgresql": [r"\bpostgres(ql)?\b", r"\bpg\b"],
    "mysql": [r"\bmysql\b"],
    "mongodb": [r"\bmongo(db)?\b"],
    "redis": [r"\bredis\b"],
    "clickhouse": [r"\bclickhouse\b"],
    "elasticsearch": [r"\belastic(search)?\b"],
    "kafka": [r"\bkafka\b"],
    "rabbitmq": [r"\brabbit(mq)?\b"],
    "docker": [r"\bdocker\b"],
    "kubernetes": [r"\bkubernetes\b", r"\bk8s\b"],
    "terraform": [r"\bterraform\b"],
    "ansible": [r"\bansible\b"],
    "aws": [r"\baws\b", r"\bamazon\s+web\s+services\b"],
    "gcp": [r"\bgcp\b", r"\bgoogle\s+cloud\b"],
    "azure": [r"\bazure\b"],
    "git": [r"\bgit\b"],
    "ci/cd": [r"\bci[\s/-]?cd\b", r"\bgitlab[\s-]?ci\b", r"\bgithub[\s-]?actions\b", r"\bjenkins\b"],
    "linux": [r"\blinux\b", r"\bubuntu\b"],
    "nginx": [r"\bnginx\b"],
    "react": [r"\breact(\.js)?\b"],
    "typescript": [r"\btypescript\b", r"\bts\b"],
    "javascript": [r"\bjavascript\b", r"\bjs\b"],
    "html/css": [r"\bhtml\b", r"\bcss\b"],
    "go": [r"\bgolang\b", r"\bgo\b"],
    "java": [r"\bjava\b"],
    "rust": [r"\brust\b"],
    "tests": [r"\bpytest\b", r"\bunittest\b", r"\btest(ing)?\b"],
    "ml": [r"\bml\b", r"\bmachine[\s-]?learning\b"],
    "data science": [r"\bdata[\s-]?science\b", r"\bds\b"],
    "pandas": [r"\bpandas\b"],
    "numpy": [r"\bnumpy\b"],
    "spark": [r"\bspark\b"],
    "airflow": [r"\bairflow\b"],
    "microservices": [r"\bmicroservic\w*\b", r"\bмикросервис\w*"],
    "monolith": [r"\bмонолит\w*"],
    "tdd": [r"\btdd\b"],
}

_COMPILED = {
    tech: [re.compile(p, re.IGNORECASE) for p in patterns] for tech, patterns in TECH_KEYWORDS.items()
}


def extract_stack(text: str) -> list[str]:
    if not text:
        return []
    found = []
    for tech, regexes in _COMPILED.items():
        if any(r.search(text) for r in regexes):
            found.append(tech)
    return found
