from pathlib import Path

from scripts.env_writer import find_duplicate_keys, load_env_values, update_env_file


def test_update_env_file_preserves_comments_and_removes_duplicates(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "# existing comment\n"
        "DATABASE_URL=postgres://old\n"
        "DATABASE_URL=postgres://duplicate\n"
        "OPENAI_API_KEY=keep-me\n",
        encoding="utf-8",
    )

    report = update_env_file(
        env_path,
        {
            "DATABASE_URL": "postgres://new",
            "REDIS_URL": "redis://127.0.0.1:6379/0",
        },
    )

    updated_text = env_path.read_text(encoding="utf-8")

    assert "# existing comment" in updated_text
    assert updated_text.count("DATABASE_URL=") == 1
    assert "DATABASE_URL=postgres://new" in updated_text
    assert "OPENAI_API_KEY=keep-me" in updated_text
    assert "REDIS_URL=redis://127.0.0.1:6379/0" in updated_text
    assert report.removed_duplicate_keys == ("DATABASE_URL",)
    assert find_duplicate_keys(env_path) == ()


def test_update_env_file_can_preserve_existing_values(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("OPENAI_API_KEY=already-set\n", encoding="utf-8")

    report = update_env_file(
        env_path,
        {"OPENAI_API_KEY": "new-secret", "LITELLM_MODEL": "openai/gpt-4o-mini"},
        preserve_existing_values=True,
    )

    values = load_env_values(env_path)

    assert values["OPENAI_API_KEY"] == "already-set"
    assert values["LITELLM_MODEL"] == "openai/gpt-4o-mini"
    assert report.preserved_keys == ("OPENAI_API_KEY",)