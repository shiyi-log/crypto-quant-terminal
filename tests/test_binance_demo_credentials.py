"""Credential storage regression tests use synthetic keys and temporary files."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import unittest

from cryptography.fernet import Fernet

from binance_demo_credentials import DemoCredentialError, DemoCredentialStore


class MemoryStore:
    def __init__(self, root):
        self.root = Path(root)
        self.row = None
        self.saved = []

    @contextmanager
    def _connection(self):
        yield self

    def execute(self, sql, args=None, **kwargs):
        if sql.startswith("INSERT INTO binance_demo_credentials"):
            env, host, cipher, fingerprint, masked, actor = args
            self.saved.append(args)
            self.row = {"environment": env, "endpoint_host": host, "ciphertext": cipher,
                        "encryption_version": 1, "fingerprint": fingerprint,
                        "masked_api_key": masked, "updated_by": actor, "updated_at": None}
        return self

    def fetchone(self):
        return self.row


class DemoCredentialTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = MemoryStore(self.temporary.name)
        self.credentials = DemoCredentialStore(self.store)
        self.key, self.secret = "synthetic-demo-api-" + "A" * 48, "synthetic-demo-secret-" + "B" * 48

    def tearDown(self):
        self.temporary.cleanup()

    def save(self):
        return self.credentials.save_credentials(self.key, self.secret, actor="admin")

    def test_empty_status_never_creates_master(self):
        self.assertFalse(self.credentials.status()["configured"])
        self.assertFalse(self.credentials.master_key_path.exists())
        with self.assertRaisesRegex(DemoCredentialError, "not_configured"):
            self.credentials.load_credentials()

    def test_database_only_ciphertext_and_master_is_private(self):
        status = self.save()
        self.assertTrue(status["configured"])
        self.assertTrue(status["decryptionAvailable"])
        self.assertEqual(os.stat(self.credentials.master_key_path).st_mode & 0o777, 0o600)
        self.assertNotIn(self.key, repr(self.store.saved))
        self.assertNotIn(self.secret, repr(self.store.saved))
        self.assertNotIn(self.key, json.dumps(status))
        loaded = self.credentials.load_credentials()
        self.assertEqual((loaded.api_key, loaded.secret), (self.key, self.secret))
        self.assertNotIn(self.key, repr(loaded))
        self.assertNotIn(self.secret, repr(loaded))

    def test_missing_master_with_existing_ciphertext_cannot_recreate_or_replace(self):
        self.save()
        self.credentials.master_key_path.unlink()
        with self.assertRaisesRegex(DemoCredentialError, "master_key_unavailable"):
            self.save()
        self.assertFalse(self.credentials.master_key_path.exists())
        self.assertFalse(self.credentials.status()["decryptionAvailable"])

    def test_wrong_valid_master_and_corrupt_ciphertext_fail_closed(self):
        self.save()
        self.credentials.master_key_path.write_bytes(Fernet.generate_key())
        self.assertFalse(self.credentials.status()["decryptionAvailable"])
        with self.assertRaisesRegex(DemoCredentialError, "cannot_decrypt"):
            self.credentials.load_credentials()
        with self.assertRaisesRegex(DemoCredentialError, "cannot_decrypt"):
            self.save()

    def test_insecure_permissions_and_symlink_are_rejected(self):
        self.save()
        path = self.credentials.master_key_path
        path.chmod(0o644)
        with self.assertRaisesRegex(DemoCredentialError, "permissions"):
            self.credentials.load_credentials()
        target = path.with_name("other.key")
        path.rename(target)
        path.symlink_to(target)
        with self.assertRaisesRegex(DemoCredentialError, "master_key_unavailable"):
            self.credentials.load_credentials()

    def test_credential_validation_and_namespace_prevent_real_url_injection(self):
        for key, secret in [("https://fapi.binance.com", self.secret), (self.key, "line\nsecret"),
                            ("", self.secret), (self.key, None)]:
            with self.subTest(key=key):
                with self.assertRaisesRegex(DemoCredentialError, "invalid_demo_credentials"):
                    self.credentials.save_credentials(key, secret, actor="admin")
        self.save()
        self.store.row["endpoint_host"] = "fapi.binance.com"
        with self.assertRaisesRegex(DemoCredentialError, "namespace_mismatch"):
            self.credentials.load_credentials()

    def test_authenticated_metadata_never_includes_secret(self):
        self.save()
        first_cipher = self.store.row["ciphertext"]
        status = self.save()
        self.assertNotEqual(first_cipher, self.store.row["ciphertext"])
        self.assertEqual(status["fingerprint"], self.credentials.status()["fingerprint"])
        self.assertEqual(set(status), {"configured", "fingerprint", "maskedApiKey", "updatedAt",
                                      "decryptionAvailable"})


if __name__ == "__main__":
    unittest.main()
