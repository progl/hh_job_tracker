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
    # Таймзона для отображения времени (в БД всё хранится в UTC через CURRENT_TIMESTAMP).
    # Москва — фиксированный UTC+3 без перехода на летнее время.
    TIMEZONE: str = "Europe/Moscow"

    # --- LLM-пайплайн (Ollama) ---
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    # Тяжёлая модель для качественного разбора (requirements/match_essay/interview_prep).
    # gemma4:26b-mlx — MoE на Apple Silicon, ~40-70 tok/s, высокое качество JSON.
    LLM_MODEL_REQUIREMENTS: str = "gemma4:26b-mlx"
    # Быстрая модель для лёгких задач (summary/salary/company_kind/soft_skills).
    # gemma4:e4b-mlx — MoE, ~60-100 tok/s на Apple Silicon.
    LLM_MODEL_FAST: str = "gemma4:e4b-mlx"
    LLM_TIMEOUT_SECONDS: float = 180.0
    LLM_TEMPERATURE: float = 0.0
    LLM_MAX_DESCRIPTION_CHARS: int = 16000  # обрезать длинные описания, чтобы не разносить context window
    # Ограничение размера контекста для generate. Дефолт ollama для llama3.1 — 131k,
    # это ест VRAM и замедляет prompt processing. Наши промпты обычно <6k токенов.
    LLM_NUM_CTX: int = 8192

    # --- RAG (опционально, extra `rag`: sqlite-vec) ---
    LLM_MODEL_EMBED: str = "bge-m3"
    EMBED_DIM: int = 1024  # размерность bge-m3; vec0-таблица фиксирована под неё
    RAG_TOP_K: int = 5
    # Обрезаем текст перед эмбеддингом — bge-m3 поддерживает до 8192 токенов,
    # но дёргать длинными сегментами медленнее. Для кириллицы ~1800 символов = ~450 токенов.
    EMBED_MAX_CHARS: int = 1800
    # Размер пачки в _job_embed_vacancies. bge-m3 в Ollama принимает массив input — один POST,
    # одна загрузка модели в память, кратное ускорение vs 1-by-1.
    EMBED_BATCH_SIZE: int = 32

    # --- Уведомления ---
    # macOS-уведомления работают без настройки. Telegram — опционально: создай бота у @BotFather,
    # узнай свой chat_id (например, через @userinfobot) и положи сюда. Канал включается на /profile.
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""

    # --- Веб-доступ (HTTP Basic) ---
    # Если оба пустые — auth выключен (локальная разработка).
    # Иначе любой роут требует Basic-credentials, включая статику и /api/health.
    WEB_AUTH_USER: str = ""
    WEB_AUTH_PASSWORD: str = ""


settings = Settings()
