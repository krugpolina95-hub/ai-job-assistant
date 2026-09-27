import pytest

from app.core.security import (
    PasswordHasher,
    generate_token,
    hash_identifier,
    hash_token,
    tokens_match,
)


@pytest.fixture(scope="module")
def hasher() -> PasswordHasher:
    return PasswordHasher(time_cost=1, memory_kib=1024, parallelism=1)


async def test_hash_is_argon2id_and_never_the_plaintext(hasher: PasswordHasher) -> None:
    password_hash = await hasher.hash("correct horse battery")

    assert password_hash.startswith("$argon2id$")
    assert "correct horse battery" not in password_hash
    assert password_hash != await hasher.hash("correct horse battery")  # salted


async def test_verify_accepts_right_and_rejects_wrong_password(hasher: PasswordHasher) -> None:
    password_hash = await hasher.hash("correct horse battery")

    assert await hasher.verify("correct horse battery", password_hash) == (True, None)
    assert (await hasher.verify("correct horse batterY", password_hash))[0] is False


async def test_verify_returns_upgraded_hash_when_parameters_changed(
    hasher: PasswordHasher,
) -> None:
    old_hash = await hasher.hash("correct horse battery")
    stronger = PasswordHasher(time_cost=2, memory_kib=2048, parallelism=1)

    valid, upgraded = await stronger.verify("correct horse battery", old_hash)

    assert valid is True
    assert upgraded is not None
    assert "m=2048,t=2,p=1" in upgraded


async def test_default_parameters_match_td_04() -> None:
    hasher = PasswordHasher(time_cost=2, memory_kib=19 * 1024, parallelism=1)

    assert "m=19456,t=2,p=1" in await hasher.hash("correct horse battery")


async def test_dummy_verification_never_raises(hasher: PasswordHasher) -> None:
    await hasher.verify_dummy("anything at all")


def test_tokens_are_unique_and_url_safe() -> None:
    tokens = {generate_token() for _ in range(1000)}

    assert len(tokens) == 1000
    assert all(len(token) >= 43 and token.isascii() and " " not in token for token in tokens)


def test_token_hash_is_stable_sha256_hex() -> None:
    token = generate_token()

    assert hash_token(token) == hash_token(token)
    assert len(hash_token(token)) == 64
    assert hash_token(token) != token


@pytest.mark.parametrize(
    ("expected", "provided", "match"),
    [
        ("abc", "abc", True),
        ("abc", "abd", False),
        ("abc", None, False),
        (None, None, False),
        ("", "", False),
    ],
)
def test_tokens_match(expected: str | None, provided: str | None, match: bool) -> None:
    assert tokens_match(expected, provided) is match


def test_log_identifier_hash_is_keyed() -> None:
    assert hash_identifier("user-1", "key-a") == hash_identifier("user-1", "key-a")
    assert hash_identifier("user-1", "key-a") != hash_identifier("user-1", "key-b")
    assert "user-1" not in hash_identifier("user-1", "key-a")
