"""``_dotenv_has_provider_key`` splits .env lines where dotenv does, so a value holding \\x1c (one
dotenv line) can't pass for a configured provider key."""

from hermes_cli.main import _dotenv_has_provider_key


def test_provider_key_on_its_own_line_counts(tmp_path):
    env = tmp_path / ".env"
    env.write_text("OTHER=1\r\nexport OPENROUTER_API_KEY=sk-or-x\n", encoding="utf-8")
    assert _dotenv_has_provider_key(env, {"OPENROUTER_API_KEY"}) is True


def test_provider_key_inside_another_value_does_not(tmp_path):
    env = tmp_path / ".env"
    env.write_text("NOTE=a\x1cOPENROUTER_API_KEY=sk-or-x\n", encoding="utf-8")
    assert _dotenv_has_provider_key(env, {"OPENROUTER_API_KEY"}) is False
