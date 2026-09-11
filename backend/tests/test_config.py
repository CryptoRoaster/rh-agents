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


# ------------------------------------------------- reasoning activation semantics

DATABASE = "postgresql+asyncpg://test_user@localhost/test_database"


def settings(monkeypatch, **env: str) -> Settings:
    monkeypatch.setenv("DATABASE_URL", DATABASE)
    for name in (
        "REASONING_PROVIDER",
        "ANTHROPIC_API_KEY",
        "ORBIT_WORKER_ENABLED",
        "REASONING_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return Settings(_env_file=None)


def test_reasoning_and_orbit_are_disabled_by_default(monkeypatch):
    configured = settings(monkeypatch)
    assert configured.reasoning_provider == "disabled"
    assert configured.orbit_worker_enabled is False
    assert configured.anthropic_api_key.get_secret_value() == ""


def test_an_ambient_api_key_alone_activates_nothing(monkeypatch):
    """A machine may hold ANTHROPIC_API_KEY for entirely unrelated purposes.

    Credential presence is not consent to spend, so it must never be the thing
    that turns reasoning on.
    """
    configured = settings(monkeypatch, ANTHROPIC_API_KEY="sk-ant-unrelated-machine-key")
    assert configured.reasoning_provider == "disabled"
    assert configured.orbit_worker_enabled is False


def test_a_real_provider_must_be_chosen_and_credentialed(monkeypatch):
    with pytest.raises(ValidationError):
        settings(monkeypatch, REASONING_PROVIDER="anthropic")
    chosen = settings(
        monkeypatch, REASONING_PROVIDER="anthropic", ANTHROPIC_API_KEY="sk-ant-explicit"
    )
    assert chosen.reasoning_provider == "anthropic"
    # Choosing a provider still does not start ORBIT.
    assert chosen.orbit_worker_enabled is False


def test_enabling_orbit_without_a_provider_fails_loudly(monkeypatch):
    with pytest.raises(ValidationError):
        settings(monkeypatch, ORBIT_WORKER_ENABLED="true")
    enabled = settings(monkeypatch, ORBIT_WORKER_ENABLED="true", REASONING_PROVIDER="fake")
    assert enabled.orbit_worker_enabled is True


def test_the_model_identifier_is_configured_never_latest(monkeypatch):
    configured = settings(monkeypatch)
    assert configured.reasoning_model == "claude-opus-5"
    assert "latest" not in configured.reasoning_model
    assert settings(monkeypatch, REASONING_MODEL="claude-sonnet-5").reasoning_model == (
        "claude-sonnet-5"
    )
