"""Validated environment configuration. No secret or business-fact defaults."""

import os
from typing import Literal
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
if os.getenv("ENVIRONMENT") != "production":
    load_dotenv(BASE_DIR / ".env")


class Settings(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)
    PROJECT_NAME: str = "ExoChain Control Tower"
    VERSION: str = "1.0.0"
    ENVIRONMENT: Literal["development", "test", "production"] = "development"
    KAFKA_BOOTSTRAP_SERVERS: str = "127.0.0.1:9092"
    KAFKA_TOPIC_AVIATION: str = "telemetry-opensky"
    KAFKA_TOPIC_MARITIME: str = "telemetry-ais"
    KAFKA_TOPIC_GRAPH_EVENTS: str = "stgnn-graph-events"
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = Field(default=6379, gt=0, le=65535)
    REDIS_DB: int = Field(default=0, ge=0)
    OPENSKY_URL: str = "https://opensky-network.org/api/states/all"
    OPENSKY_POLL_INTERVAL: int = 10
    AISSTREAM_URL: str = "wss://stream.aisstream.io/v0/stream"
    AISSTREAM_API_KEY: SecretStr = SecretStr("")
    GROQ_API_KEY: SecretStr = SecretStr("")
    GROQ_MODEL: str = "llama-3.1-8b-instant"
    TAVILY_API_KEY: SecretStr = SecretStr("")
    STGNN_NUM_NODES: int = 50
    STGNN_TEMPORAL_WINDOW: int = 12
    PROCUREMENT_MAX_BUDGET_USD: float = Field(default=35000, gt=0)
    APPROVAL_THRESHOLD_USD: float = Field(default=15000, gt=0)
    APPROVAL_TTL_SECONDS: int = Field(default=3600, gt=0)
    ALLOW_AUTOMATIC_APPROVAL: bool = False
    AIS_MAX_AGE_SECONDS: int = Field(default=1800, gt=0)
    WEATHER_MAX_AGE_SECONDS: int = Field(default=3600, gt=0)
    OCEAN_MAX_AGE_SECONDS: int = Field(default=129600, gt=0)
    PORT_MAX_AGE_SECONDS: int = Field(default=604800, gt=0)
    BUSINESS_MAX_AGE_SECONDS: int = Field(default=86400, gt=0)
    SOLVER_TIMEOUT_SECONDS: float = Field(default=10, gt=0, le=120)
    PROVIDER_TIMEOUT_SECONDS: float = Field(default=20, gt=0, le=120)
    PROVIDER_WORKERS: int = Field(default=6, gt=0, le=12)
    TELEMETRY_MAX_VESSELS: int = Field(default=200, gt=0, le=10000)
    RUN_DB_PATH: str = "data/decision_runs.db"
    BUSINESS_DATA_PATH: str = ""
    BUSINESS_API_URL: str = ""
    BUSINESS_API_TOKEN: SecretStr = SecretStr("")
    BUSINESS_SOURCE_ID: str = ""
    BUSINESS_SOURCE_TRUST: Literal["UNVERIFIED", "AUTHORITATIVE"] = "UNVERIFIED"
    # Legacy configuration accepted for old installations; API authentication is removed.
    OPERATOR_API_TOKEN: SecretStr = SecretStr("")
    APPROVER_API_TOKEN: SecretStr = SecretStr("")
    ERP_URL: str = ""
    ERP_TOKEN: SecretStr = SecretStr("")
    ERP_MODE: str = "unavailable"
    ERP_HEALTH_PATH: str = ""
    CORS_ORIGINS: str = "http://localhost:3000,http://127.0.0.1:3000"
    TRUSTED_HOSTS: str = "localhost,127.0.0.1,testserver"
    STGNN_MODEL_MANIFEST: str = ""

    @model_validator(mode="after")
    def validate_startup(self):
        if self.ERP_MODE not in {"unavailable", "simulation", "http"}:
            raise ValueError("invalid ERP_MODE")
        if self.ERP_URL and not self.ERP_URL.startswith("https://"):
            raise ValueError("HTTP ERP requires HTTPS")
        if self.ERP_URL:
            from urllib.parse import urlsplit

            parsed_erp = urlsplit(self.ERP_URL)
            if (
                not parsed_erp.hostname
                or parsed_erp.username
                or parsed_erp.password
                or parsed_erp.query
                or parsed_erp.fragment
            ):
                raise ValueError(
                    "ERP base URL must not contain credentials, query or fragment"
                )
        if self.BUSINESS_API_URL and not self.BUSINESS_API_URL.startswith("https://"):
            raise ValueError("business API requires HTTPS")
        if self.ENVIRONMENT == "production":
            from urllib.parse import urlsplit

            if self.ERP_MODE == "simulation":
                raise ValueError("Production cannot use ERP simulation")
            if "*" in self.TRUSTED_HOSTS or not self.TRUSTED_HOSTS:
                raise ValueError("Production requires explicit trusted hosts")
            for origin in self.CORS_ORIGINS.split(","):
                parsed = urlsplit(origin)
                if (
                    parsed.scheme != "https"
                    or not parsed.hostname
                    or parsed.username
                    or parsed.password
                    or parsed.query
                    or parsed.fragment
                    or parsed.path not in {"", "/"}
                ):
                    raise ValueError(
                        "Production CORS origins must be explicit HTTPS origins"
                    )
        return self


def environment_values():
    values = {
        key: os.environ[key] for key in Settings.model_fields if key in os.environ
    }
    for key in Settings.model_fields:
        secret_path = os.environ.get(key + "_FILE")
        if secret_path:
            if key in values and values[key]:
                raise ValueError(f"Configure either {key} or {key}_FILE")
            try:
                values[key] = Path(secret_path).read_text(encoding="utf-8").strip()
            except OSError:
                raise ValueError(f"Cannot read configured {key}_FILE") from None
    return values


settings = Settings.model_validate(environment_values())
