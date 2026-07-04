from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


BACKEND_DIR = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "DataAgent"
    environment: str = "development"
    secret_key: str = "development-only-change-me"
    database_backend: str = "mysql"
    database_path: str = "storage/data_agent.db"
    session_files_path: str = "storage/session_files"
    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    mysql_user: str = "dataagent"
    mysql_password: str = "dataagent123"
    mysql_database: str = "dataagent"
    frontend_origin: str = "http://localhost:5173"

    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = ""
    api_mode: str = "system"
    system_llm_api_key: str = ""
    system_llm_base_url: str = ""
    system_llm_model: str = ""

    embedding_api_key: str = ""
    embedding_base_url: str = ""
    embedding_model: str = "BAAI/bge-m3"
    system_embedding_api_key: str = ""
    system_embedding_base_url: str = ""
    system_embedding_model: str = ""
    vector_store: str = "qdrant"
    qdrant_url: str = ""
    qdrant_api_key: str = ""
    qdrant_path: str = "storage/qdrant"
    faiss_path: str = "storage/faiss"
    milvus_uri: str = ""
    milvus_token: str = ""
    milvus_database: str = ""
    semantic_search_timeout_seconds: float = Field(default=1.0, ge=0.1, le=30.0)
    mcp_servers: str = ""

    max_upload_mb: int = Field(default=100, ge=1, le=200)
    query_row_limit: int = Field(default=500, ge=10, le=5000)
    sql_timeout_seconds: int = Field(default=15, ge=1, le=120)

    @property
    def database_file(self) -> Path:
        path = Path(self.database_path)
        return path if path.is_absolute() else BACKEND_DIR / path

    @property
    def session_files_directory(self) -> Path:
        path = Path(self.session_files_path)
        return path if path.is_absolute() else BACKEND_DIR / path

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_api_key and self.llm_model)

    @property
    def embedding_configured(self) -> bool:
        return bool(self.embedding_api_key and self.embedding_base_url and self.embedding_model)

    @property
    def qdrant_directory(self) -> Path:
        path = Path(self.qdrant_path)
        return path if path.is_absolute() else BACKEND_DIR / path

    @property
    def faiss_directory(self) -> Path:
        path = Path(self.faiss_path)
        return path if path.is_absolute() else BACKEND_DIR / path


def get_settings() -> Settings:
    # MVP 开发模式下每次读取 .env，修改模型配置后无需重启服务。
    # 生产环境应改由 Secret Manager 注入，并在部署时重启实例。
    return Settings()
