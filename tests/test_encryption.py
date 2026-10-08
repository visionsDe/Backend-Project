"""Hash-sidecar + Fernet round-trip."""
from app.utils.encryption import (
    decrypt_data,
    encrypt_data,
    hash_sort_string,
    hash_string,
)


def test_fernet_round_trip():
    cipher = encrypt_data("Something confidential")
    assert cipher != "Something confidential"
    assert decrypt_data(cipher) == "Something confidential"


def test_decrypt_invalid_token_returns_input():
    """``decrypt_data`` is used on columns that may contain legacy
    plaintext; invalid tokens must come back as-is, not raise."""
    assert decrypt_data("not-a-fernet-token") == "not-a-fernet-token"


def test_hash_string_is_deterministic():
    assert hash_string("Alice") == hash_string("alice") == hash_string("  ALICE  ")
    assert hash_string("Alice") != hash_string("Bob")
    assert hash_string("") == ""


def test_hash_sort_string_preserves_first_char():
    """First-char prefix means an ``ORDER BY hash`` on an encrypted
    column still returns roughly alphabetical results."""
    h_alice = hash_sort_string("Alice")
    h_amy = hash_sort_string("Amy")
    h_bob = hash_sort_string("Bob")
    assert h_alice.startswith("a")
    assert h_amy.startswith("a")
    assert h_bob.startswith("b")
    assert sorted([h_bob, h_alice, h_amy]) == [h_alice, h_amy, h_bob]
