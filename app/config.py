from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://support:support@localhost:5432/support"
    db_pool_size: int = 10
    db_null_pool: bool = False

    anthropic_api_key: str = ""
    llm_model: str = "claude-opus-5-5"
    llm_effort: str = "low"
    llm_refusal_fallback: bool = True
    llm_max_tokens: int = 8000
    llm_timeout_seconds: float = 120.0
    llm_probe_timeout_seconds: float = 10.0
    llm_rate_limit_per_minute: int = 60
    llm_stub_min_delay: float = 5.0
    llm_stub_max_delay: float = 30.0

    generation_concurrency: int = 30
    generation_max_attempts: int = 5
    generation_lease_seconds: int = 300

    send_concurrency: int = 10
    send_max_attempts: int = 10
    send_timeout_seconds: float = 10.0
    send_lease_seconds: int = 60

    worker_poll_interval: float = 1.0
    kb_top_k: int = 3

    public_base_url: str = "http://localhost:8000"
    reply_url_rewrite_from: str = ""
    reply_url_rewrite_to: str = ""

    operator_username: str = "operator"
    operator_password: str = ""


settings = Settings()
