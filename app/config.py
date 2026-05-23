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


settings = Settings()
