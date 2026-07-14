from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql://postgres@localhost:5433/tprm_db"
    use_db: bool = False
    log_level: str = "INFO"
    rate_limit_static: float = 2.0
    rate_limit_dynamic: float = 3.0
    concurrency: int = 3
    rss_interval_minutes: int = 30
    html_static_interval_minutes: int = 60
    html_dynamic_interval_hours: int = 6
    tender_interval_hours: int = 12
    playwright_headless: bool = True
    playwright_timeout_ms: int = 20000
    groq_api_key: str = ""

    class Config:
        env_file = ".env"
        case_sensitive = False
        extra = "ignore"


settings = Settings()
