"""Independent encrypted Binance Demo credentials; never imports live config.

Only ciphertext and non-secret metadata enter PostgreSQL. A separately stored
0600 local master key is required to decrypt. Losing that key fails closed;
the code never silently replaces it while an encrypted credential exists.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

ENVIRONMENT = "binance_demo"
ENDPOINT_HOST = "demo-fapi.binance.com"
_CREDENTIAL = re.compile(r"[A-Za-z0-9_-]{16,512}\Z")


class DemoCredentialError(RuntimeError):
    """Public error codes deliberately carry no request or credential text."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class DemoCredentials:
    api_key: str = field(repr=False)
    secret: str = field(repr=False)


class DemoCredentialStore:
    def __init__(self, data_store, *, master_key_path: str | Path | None = None):
        self.data_store = data_store
        root = Path(data_store.root)
        self.master_key_path = Path(master_key_path or root / "auth" / "demo_master.key")
        self._lock = threading.RLock()

    def ensure_schema(self):
        """Explicit setup only. Read/write requests never install schema."""
        sql = Path(__file__).with_name("database") / "demo_credentials_schema.sql"
        with self.data_store._connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                         ("binance-demo-credentials-schema",))
            conn.execute(sql.read_text(encoding="utf-8"), prepare=False)

    def _row(self, conn=None):
        query = "SELECT * FROM binance_demo_credentials WHERE environment=%s"
        if conn is not None:
            return conn.execute(query, (ENVIRONMENT,)).fetchone()
        with self.data_store._connection() as connection:
            return connection.execute(query, (ENVIRONMENT,)).fetchone()

    def _fernet(self, *, allow_create: bool = False) -> Fernet:
        path = self.master_key_path
        if allow_create:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                                     getattr(os, "O_NOFOLLOW", 0), 0o600)
            except FileExistsError:
                pass
            except OSError:
                raise DemoCredentialError("demo_master_key_unavailable") from None
            else:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(Fernet.generate_key())
                    stream.flush()
                    os.fsync(stream.fileno())
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as stream:
                metadata = os.fstat(stream.fileno())
                if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                        or stat.S_IMODE(metadata.st_mode) != 0o600):
                    raise DemoCredentialError("demo_master_key_permissions")
                key = stream.read(1024)
            return Fernet(key)
        except DemoCredentialError:
            raise
        except (OSError, ValueError, TypeError):
            raise DemoCredentialError("demo_master_key_unavailable") from None

    def status(self) -> dict:
        row = self._row()
        if row is None:
            return {"configured": False, "fingerprint": None, "maskedApiKey": None,
                    "updatedAt": None, "decryptionAvailable": False}
        try:
            self._decrypt_row(row, self._fernet())
            available = True
        except DemoCredentialError:
            available = False
        updated = row.get("updated_at")
        return {"configured": True, "fingerprint": row["fingerprint"],
                "maskedApiKey": row["masked_api_key"],
                "updatedAt": updated.isoformat() if hasattr(updated, "isoformat") else updated,
                "decryptionAvailable": available}

    def save_credentials(self, api_key: str, secret: str, *, actor: str) -> dict:
        if (not isinstance(api_key, str) or not isinstance(secret, str)
                or not _CREDENTIAL.fullmatch(api_key) or not _CREDENTIAL.fullmatch(secret)):
            raise DemoCredentialError("invalid_demo_credentials")
        if not isinstance(actor, str) or not actor or len(actor) > 128:
            raise DemoCredentialError("invalid_credential_actor")
        with self._lock, self.data_store._connection() as conn:
            # Serialize first creation across processes too. An existing row
            # requires a working key even when replacing credentials.
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                         ("binance-demo-credentials-save",))
            existing = self._row(conn)
            cipher = self._fernet(allow_create=existing is None)
            if existing is not None:
                self._decrypt_row(existing, cipher)
            payload = json.dumps({"version": 1, "environment": ENVIRONMENT,
                                  "endpoint": ENDPOINT_HOST,
                                  "apiKey": api_key, "secret": secret},
                                 separators=(",", ":")).encode()
            ciphertext = cipher.encrypt(payload).decode("ascii")
            fingerprint = hashlib.sha256(api_key.encode()).hexdigest()[:16]
            masked = "••••" + api_key[-4:]
            conn.execute("""INSERT INTO binance_demo_credentials
                (environment,endpoint_host,ciphertext,encryption_version,
                 fingerprint,masked_api_key,updated_by)
                VALUES (%s,%s,%s,1,%s,%s,%s)
                ON CONFLICT (environment) DO UPDATE SET
                ciphertext=EXCLUDED.ciphertext,fingerprint=EXCLUDED.fingerprint,
                masked_api_key=EXCLUDED.masked_api_key,updated_by=EXCLUDED.updated_by,
                updated_at=CURRENT_TIMESTAMP""",
                         (ENVIRONMENT, ENDPOINT_HOST, ciphertext, fingerprint, masked, actor))
        return self.status()

    @staticmethod
    def _decrypt_row(row, cipher: Fernet) -> DemoCredentials:
        if row.get("environment") != ENVIRONMENT or row.get("endpoint_host") != ENDPOINT_HOST:
            raise DemoCredentialError("demo_credential_namespace_mismatch")
        try:
            payload = json.loads(cipher.decrypt(row["ciphertext"].encode("ascii")))
            if (payload.get("version") != 1 or payload.get("environment") != ENVIRONMENT
                    or payload.get("endpoint") != ENDPOINT_HOST
                    or not _CREDENTIAL.fullmatch(payload["apiKey"])
                    or not _CREDENTIAL.fullmatch(payload["secret"])):
                raise ValueError
            return DemoCredentials(payload["apiKey"], payload["secret"])
        except (InvalidToken, ValueError, TypeError, KeyError, UnicodeError, AttributeError):
            raise DemoCredentialError("demo_credentials_cannot_decrypt") from None

    def load_credentials(self) -> DemoCredentials:
        row = self._row()
        if row is None:
            raise DemoCredentialError("demo_credentials_not_configured")
        return self._decrypt_row(row, self._fernet())
