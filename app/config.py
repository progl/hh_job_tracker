from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    HH_BASE_URL: str = "https://hh.ru"
    HH_USER_AGENT: str
    HH_SEC_CH_UA: str
    HH_SEC_CH_UA_PLATFORM: str
    HH_SEC_CH_UA_MOBILE: str = "?0"
    HH_COOKIE: str = ""

    HH_MIN_DELAY_SEC: float = 3.0
    HH_MAX_DELAY_SEC: float = 6.0
    HH_REQUESTS_PER_MIN_LIMIT: int = 25
    HH_REST_AFTER_REQUESTS: int = 50
    HH_REST_DURATION_SEC: float = 45.0

    DB_PATH: str = "data/hh.db"

    # --- LLM-пайплайн (Ollama) ---
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    LLM_MODEL_REQUIREMENTS: str = "qwen3:14b"
    # «Быстрая» модель для лёгких задач (summary/salary/company_kind/soft_skills) —
    # тяжёлые (requirements/match_essay/interview_prep) остаются на REQUIREMENTS.
    # На M1 Max llama3.1:8b ≈ 30 tok/s (vs ~80 у 3.2:3b), но качество JSON для русского
    # намного стабильнее. На коротких prompt'ах (summary) — 3-7 сек.
    # Альтернативы быстрее (через UI): llama3.2:latest (3b), qwen3:1.7b, phi4-mini.
    LLM_MODEL_FAST: str = "llama3.1:8b"
    LLM_TIMEOUT_SECONDS: float = 180.0
    LLM_TEMPERATURE: float = 0.0
    LLM_MAX_DESCRIPTION_CHARS: int = 16000  # обрезать длинные описания, чтобы не разносить context window


settings = Settings()
