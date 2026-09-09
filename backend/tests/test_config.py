import pytest
from pydantic import ValidationError

from src.core.config import Settings


def test_database_url_is_required(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("database_url", raising=False)
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None)
    assert any(
        item["loc"] == ("database_url",) and item["type"] == "missing"
        for item in error.value.errors()
    )


def test_database_url_comes_from_environment(monkeypatch):
    url = "postgresql+asyncpg://test_user@localhost/test_database"
    monkeypatch.setenv("DATABASE_URL", url)
    assert Settings(_env_file=None).database_url == url


def test_database_url_comes_from_dotenv(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("database_url", raising=False)
    env = tmp_path / ".env"
    env.write_text("DATABASE_URL=postgresql+asyncpg://test_user@localhost/test_database\n")
    assert Settings(_env_file=env).database_url.endswith("/test_database")


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "\t\n",
        "not-a-database-url",
        "sqlite+aiosqlite:///:memory:",
        "sqlite:///test.db",
        "postgresql://test_user@localhost/test_database",
        "postgresql+psycopg://test_user@localhost/test_database",
        "postgresql+psycopg2://test_user@localhost/test_database",
        "postgresql+asyncpg://test_user@localhost",
        "postgresql+asyncpg://test_user@localhost/",
        "postgresql+asyncpg://runner@/?host=/var/run/postgresql",
        "postgresql+asyncpg://test_user@localhost/%20",
        "postgresql+asyncpg://test_user@localhost:invalid/test_database",
        "postgresql+asyncpg://test_user@localhost:65536/test_database",
        "postgresql+asyncpg://test_user@localhost:0/test_database",
        "postgresql+asyncpg://test_user@localhost/test_database#fragment",
        " postgresql+asyncpg://test_user@localhost/test_database",
    ],
)
def test_invalid_runtime_database_url_is_rejected(url):
    with pytest.raises(ValidationError, match="database_url"):
        Settings(_env_file=None, database_url=url)


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://test_user@localhost:5432/test_database",
        "postgresql+asyncpg://runner@/rh_agents_test?host=/var/run/postgresql",
    ],
)
def test_supported_runtime_database_urls_are_preserved(url):
    assert Settings(_env_file=None, database_url=url).database_url == url
