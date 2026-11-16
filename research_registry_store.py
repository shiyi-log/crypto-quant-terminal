"""Durable research declarations and once-only holdout tickets, without running jobs.

A committed registry record proves stored identity, never source authenticity or
complete statistics. No network, worker start, or production authorization.
"""

from __future__ import annotations

from decimal import Decimal
import hashlib
import json
import re
from urllib.parse import urlsplit

from demo_store import DemoStore, _scrub_text
from research_protocol import FrozenResearchEvidence, freeze_experiment_family, evaluate_candidate

ID = re.compile(r"[A-Za-z0-9_.:/-]{1,200}\Z")
SHA = re.compile(r"[a-f0-9]{64}\Z")
TERMINAL = frozenset({"completed", "failed", "canceled", "not_estimable"})
HOLDOUT_CLOSED = frozenset({"completed", "failed"})
HOLDOUT_RESULT_BLOCKED = HOLDOUT_CLOSED | {"unknown"}


def _id(value):
    if not isinstance(value, str) or not ID.fullmatch(value) or "://" in value:
        raise ValueError("invalid safe registry identity")
    return value


def _sha(value):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise ValueError("invalid registry SHA-256")
    return value


def _time(value):
    if type(value) is not int or not 0 <= value < 2**63:
        raise ValueError("invalid registry timestamp")
    return value


def _uri(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 1000 or any(ord(c) < 33 for c in value):
        raise ValueError("invalid artifact/source URI")
    if _scrub_text(value) != value:
        raise ValueError("credential-bearing URI forbidden")
    parsed = urlsplit(value)
    if (
        parsed.scheme
        not in {"archive", "artifact", "dataset", "quality", "file", "https", "s3", "gs"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or "@" in value
    ):
        raise ValueError("credential-bearing or unsupported URI forbidden")
    return value


def _clean(value, depth=0):
    if depth > 32:
        raise ValueError("registry JSON too deep")
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if (
                not isinstance(key, str)
                or len(key) > 200
                or any(ord(c) < 32 for c in key)
                or _scrub_text(key) != key
                or "://" in key
            ):
                raise ValueError("invalid registry JSON key")
            result[key] = _clean(item, depth + 1)
        return result
    if isinstance(value, list):
        if len(value) > 10000:
            raise ValueError("registry JSON list too large")
        return [_clean(item, depth + 1) for item in value]
    if isinstance(value, Decimal):
        if (
            not value.is_finite()
            or len(value.as_tuple().digits) > 96
            or abs(value.as_tuple().exponent) > 192
        ):
            raise ValueError("invalid exact registry decimal")
        result = format(value, "f")
        return result.rstrip("0").rstrip(".") if "." in result else result
    if isinstance(value, str):
        if len(value) > 1000000 or any(ord(c) < 32 for c in value) or _scrub_text(value) != value:
            raise ValueError("invalid registry string")
        if "://" in value:
            _uri(value)
        return value
    if value is None or type(value) in (bool, int):
        if type(value) is int and value.bit_length() > 256:
            raise ValueError("registry integer too large")
        return value
    raise ValueError("unsupported exact registry JSON type")


def _canonical(value):
    return json.dumps(
        value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False
    )


def _hash(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


class ResearchRegistryStore(DemoStore):
    """One account lock serializes registrations, attempts and final consumption."""

    def __init__(self, data_store, account_id):
        self.account_id = _id(account_id)
        super().__init__(data_store, "research-registry")

    def _lock(self, conn):
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))", ("research-registry:" + self.account_id,)
        )

    @staticmethod
    def _verified(row):
        if row is None:
            return None
        payload = row["payload"]
        if _hash(payload) != row["payload_sha256"]:
            raise RuntimeError("research registry fact integrity failure")
        return payload

    def _version(self, conn, kind, version_id):
        row = conn.execute(
            "SELECT * FROM refactor_research_versions WHERE account_id=%s AND kind=%s AND version_id=%s",
            (self.account_id, kind, version_id),
        ).fetchone()
        value = self._verified(row)
        if value is None:
            raise ValueError("registered version reference missing")
        return value

    def register_version(self, kind, version):
        if kind not in {"dataset", "factor", "strategy", "exit"}:
            raise ValueError("unknown research version kind")
        value = _clean(version)
        required = {
            "version_id",
            "sha256",
            "source_uri",
            "source_sha256",
            "captured_ms",
            "quality_ref",
            "definition",
        }
        if (
            not isinstance(value, dict)
            or set(value) != required
            or not isinstance(value["definition"], dict)
        ):
            raise ValueError("complete explicit version facts required")
        _id(value["version_id"])
        _sha(value["sha256"])
        _sha(value["source_sha256"])
        _uri(value["source_uri"])
        _id(value["quality_ref"])
        _time(value["captured_ms"])
        if kind == "dataset":
            definition = value["definition"]
            if set(definition) != {"start_ms", "end_ms"} or _time(definition["end_ms"]) <= _time(
                definition["start_ms"]
            ):
                raise ValueError("dataset version time range required")
        with self.store._connection() as conn:
            self._lock(conn)
            row = conn.execute(
                "SELECT * FROM refactor_research_versions WHERE account_id=%s AND kind=%s AND version_id=%s",
                (self.account_id, kind, value["version_id"]),
            ).fetchone()
            if row:
                if self._verified(row) != value:
                    raise ValueError("immutable research version conflict")
            else:
                conn.execute(
                    "INSERT INTO refactor_research_versions(account_id,kind,version_id,payload_sha256,payload) "
                    "VALUES(%s,%s,%s,%s,%s)",
                    (
                        self.account_id,
                        kind,
                        value["version_id"],
                        _hash(value),
                        self.store._json(value),
                    ),
                )
        return value

    def _family(self, conn, family_id):
        row = conn.execute(
            "SELECT * FROM refactor_research_families WHERE account_id=%s AND family_id=%s",
            (self.account_id, family_id),
        ).fetchone()
        value = self._verified(row)
        if value and (
            _hash(value["frozen"]) != value["family_sha256"]
            or row["family_sha256"] != value["family_sha256"]
        ):
            raise RuntimeError("frozen family integrity failure")
        return value

    def get_family(self, family_id):
        with self.store._connection() as conn:
            return self._family(conn, _id(family_id))

    def register_family(self, spec, *, dataset_version_id, strategy_version_id):
        spec = _clean(spec)
        frozen = freeze_experiment_family(spec)
        dataset_version_id, strategy_version_id = _id(dataset_version_id), _id(strategy_version_id)
        value = {
            "frozen": frozen.to_dict(),
            "family_sha256": frozen.sha256,
            "dataset_version_id": dataset_version_id,
            "strategy_version_id": strategy_version_id,
            "source_authenticity_verified": False,
        }
        with self.store._connection() as conn:
            self._lock(conn)
            dataset = self._version(conn, "dataset", dataset_version_id)
            strategy = self._version(conn, "strategy", strategy_version_id)
            if (
                dataset["sha256"] != spec["dataset"]["sha256"]
                or dataset["source_uri"] != spec["dataset"]["source_ref"]
                or dataset["definition"]
                != {key: spec["dataset"][key] for key in ("start_ms", "end_ms")}
                or strategy["sha256"] != spec["identities"]["strategy_hash"]
            ):
                raise ValueError("family registered version identity mismatch")
            for kind, axis in (("factor", "factor_sets"), ("exit", "exit_sets")):
                for reference in spec[axis]:
                    if self._version(conn, kind, reference["id"])["sha256"] != reference["sha256"]:
                        raise ValueError("family frozen version identity mismatch")
            old = self._family(conn, spec["family_id"])
            if old:
                if old != value:
                    raise ValueError("immutable research family conflict")
            else:
                conn.execute(
                    "INSERT INTO refactor_research_families(account_id,family_id,family_sha256,payload_sha256,payload) "
                    "VALUES(%s,%s,%s,%s,%s)",
                    (
                        self.account_id,
                        spec["family_id"],
                        frozen.sha256,
                        _hash(value),
                        self.store._json(value),
                    ),
                )
        return value

    def _attempt(self, conn, attempt_id):
        row = conn.execute(
            "SELECT * FROM refactor_research_attempts WHERE account_id=%s AND attempt_id=%s",
            (self.account_id, attempt_id),
        ).fetchone()
        value = self._verified(row)
        if value is None:
            raise ValueError("registered attempt missing")
        events = conn.execute(
            "SELECT * FROM refactor_research_attempt_events WHERE account_id=%s AND attempt_id=%s "
            "ORDER BY sequence_id",
            (self.account_id, attempt_id),
        ).fetchall()
        status, last = "registered", value["registered_ms"]
        for event in events:
            data = self._verified(event)
            kind = data["event_type"]
            if (
                data["attempt_id"] != attempt_id
                or data["event_id"] != event["event_id"]
                or data["observed_ms"] < last
                or data["observed_ms"] != event["observed_ms"]
                or kind != event["event_type"]
                or (kind in TERMINAL and status != "started")
                or (kind == "started" and status != "registered")
                or kind not in TERMINAL | {"started"}
            ):
                raise RuntimeError("research attempt event sequence invalid")
            status, last = kind, data["observed_ms"]
        return {
            **value,
            "status": status,
            "last_ms": last,
            "events": [self._verified(event) for event in events],
        }

    def _insert_attempt(self, conn, value):
        old = conn.execute(
            "SELECT * FROM refactor_research_attempts WHERE account_id=%s AND attempt_id=%s",
            (self.account_id, value["attempt_id"]),
        ).fetchone()
        if old:
            if self._verified(old) != value:
                raise ValueError("immutable attempt identity conflict")
            return self._attempt(conn, value["attempt_id"])
        conn.execute(
            "INSERT INTO refactor_research_attempts(account_id,attempt_id,family_id,claim_id,config_id,payload_sha256,payload) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (
                self.account_id,
                value["attempt_id"],
                value["family_id"],
                value["claim_id"],
                value["config_id"],
                _hash(value),
                self.store._json(value),
            ),
        )
        return self._attempt(conn, value["attempt_id"])

    def register_attempt(self, family_id, attempt_id, config_id, *, producer_id, registered_ms):
        family_id, attempt_id, producer_id = _id(family_id), _id(attempt_id), _id(producer_id)
        _sha(config_id)
        _time(registered_ms)
        with self.store._connection() as conn:
            self._lock(conn)
            family = self._family(conn, family_id)
            if not family or config_id not in {
                row["config_id"] for row in family["frozen"]["configurations"]
            }:
                raise ValueError("registered family configuration missing")
            frozen = family["frozen"]
            if registered_ms < max(frozen["registered_ms"], frozen["dataset"]["end_ms"]):
                raise ValueError("dataset not available at attempt registration")
            value = dict(
                attempt_id=attempt_id,
                family_id=family_id,
                config_id=config_id,
                producer_id=producer_id,
                registered_ms=registered_ms,
                phase="validation",
                claim_id=None,
                family_sha256=family["family_sha256"],
                identities=frozen["identities"],
            )
            result = self._insert_attempt(conn, value)
        return result

    def _attempt_event(self, conn, attempt_id, event_type, event_id, observed_ms, error_code=None):
        value = {
            "attempt_id": _id(attempt_id),
            "event_type": event_type,
            "event_id": _id(event_id),
            "observed_ms": _time(observed_ms),
            "error_code": None if error_code is None else _id(error_code),
        }
        if event_type not in TERMINAL | {"started"} or (
            event_type in {"failed", "not_estimable"} and error_code is None
        ):
            raise ValueError("explicit attempt event/error required")
        old = conn.execute(
            "SELECT * FROM refactor_research_attempt_events WHERE account_id=%s AND event_id=%s",
            (self.account_id, event_id),
        ).fetchone()
        if old:
            if self._verified(old) != value:
                raise ValueError("immutable attempt event conflict")
            return value
        current = self._attempt(conn, attempt_id)
        if current["status"] in TERMINAL:
            raise ValueError("attempt terminal state cannot change")
        if event_type in TERMINAL and current["status"] != "started":
            raise ValueError("attempt must be started before terminal event")
        if observed_ms < current["last_ms"] or (
            event_type == "started" and current["status"] != "registered"
        ):
            raise ValueError("attempt event ordering conflict")
        conn.execute(
            "INSERT INTO refactor_research_attempt_events(account_id,attempt_id,event_id,event_type,observed_ms,payload_sha256,payload) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (
                self.account_id,
                attempt_id,
                event_id,
                event_type,
                observed_ms,
                _hash(value),
                self.store._json(value),
            ),
        )
        return value

    def record_attempt_event(
        self, attempt_id, event_type, *, event_id, observed_ms, error_code=None
    ):
        with self.store._connection() as conn:
            self._lock(conn)
            result = self._attempt_event(
                conn, attempt_id, event_type, event_id, observed_ms, error_code
            )
        return result

    def attempts(self, family_id):
        with self.store._connection() as conn:
            self._lock(conn)
            rows = conn.execute(
                "SELECT attempt_id FROM refactor_research_attempts WHERE account_id=%s AND family_id=%s "
                "ORDER BY sequence_id",
                (self.account_id, _id(family_id)),
            ).fetchall()
            return [self._attempt(conn, row["attempt_id"]) for row in rows]

    def register_artifact(self, artifact):
        value = _clean(artifact)
        required = {"artifact_id", "attempt_id", "uri", "sha256", "media_type", "created_ms"}
        if not isinstance(value, dict) or set(value) != required:
            raise ValueError("complete artifact identity required")
        _id(value["artifact_id"])
        _id(value["attempt_id"])
        _uri(value["uri"])
        _sha(value["sha256"])
        _time(value["created_ms"])
        if value["media_type"] not in {
            "application/json",
            "text/csv",
            "application/octet-stream",
            "image/png",
            "application/pdf",
        }:
            raise ValueError("unsupported artifact media type")
        with self.store._connection() as conn:
            self._lock(conn)
            attempt = self._attempt(conn, value["attempt_id"])
            if attempt["status"] != "completed" or value["created_ms"] < attempt["last_ms"]:
                raise ValueError("artifact requires completed attempt and later timestamp")
            row = conn.execute(
                "SELECT * FROM refactor_research_artifacts WHERE account_id=%s AND artifact_id=%s",
                (self.account_id, value["artifact_id"]),
            ).fetchone()
            if row:
                if self._verified(row) != value:
                    raise ValueError("immutable artifact identity conflict")
            else:
                conn.execute(
                    "INSERT INTO refactor_research_artifacts(account_id,artifact_id,attempt_id,payload_sha256,payload) "
                    "VALUES(%s,%s,%s,%s,%s)",
                    (
                        self.account_id,
                        value["artifact_id"],
                        value["attempt_id"],
                        _hash(value),
                        self.store._json(value),
                    ),
                )
        return value

    def _claim(self, conn, family_id):
        row = conn.execute(
            "SELECT * FROM refactor_research_holdout_claims WHERE account_id=%s AND family_id=%s",
            (self.account_id, family_id),
        ).fetchone()
        value = self._verified(row)
        if value is None:
            return None
        events = conn.execute(
            "SELECT * FROM refactor_research_holdout_events WHERE account_id=%s AND claim_id=%s "
            "ORDER BY sequence_id",
            (self.account_id, value["claim_id"]),
        ).fetchall()
        status = "consumed"
        last = value["claimed_ms"]
        for event in events:
            payload = self._verified(event)
            if (
                payload["event_id"] != event["event_id"]
                or payload["event_type"] != event["event_type"]
                or payload["observed_ms"] != event["observed_ms"]
                or payload["observed_ms"] < last
                or event["claim_id"] != value["claim_id"]
                or status in HOLDOUT_CLOSED
            ):
                raise RuntimeError("holdout event sequence integrity failure")
            status = payload["event_type"]
            last = payload["observed_ms"]
        result = conn.execute(
            "SELECT * FROM refactor_research_candidate_results WHERE account_id=%s AND claim_id=%s",
            (self.account_id, value["claim_id"]),
        ).fetchone()
        if result:
            payload = self._verified(result)
            if payload["claim_id"] != value["claim_id"]:
                raise RuntimeError("candidate result claim mismatch")
            status = "completed"
        return {
            "claim": value,
            "state": status,
            "events": [self._verified(event) for event in events],
            "result": None if result is None else self._verified(result),
        }

    def get_holdout(self, family_id):
        with self.store._connection() as conn:
            self._lock(conn)
            return self._claim(conn, _id(family_id))

    def claim_holdout(
        self,
        *,
        family_id,
        claim_id,
        base_config_id,
        base_attempt_id,
        stress_attempt_id,
        producer_id,
        claimed_ms,
    ):
        family_id, claim_id = _id(family_id), _id(claim_id)
        producer_id, base_attempt_id, stress_attempt_id = map(
            _id, (producer_id, base_attempt_id, stress_attempt_id)
        )
        _sha(base_config_id)
        _time(claimed_ms)
        if base_attempt_id == stress_attempt_id:
            raise ValueError("base and stress attempts need distinct identities")
        receipt = None
        with self.store._connection() as conn:
            self._lock(conn)
            old = self._claim(conn, family_id)
            family = self._family(conn, family_id)
            if family is None:
                raise ValueError("registered family required")
            frozen = family["frozen"]
            configs = {row["config_id"]: row for row in frozen["configurations"]}
            base = configs.get(base_config_id)
            if base is None or base["cost_scenario"] != "base":
                raise ValueError("final base configuration required")
            stress = next(
                (
                    row
                    for row in frozen["configurations"]
                    if row["cost_scenario"] == "stress"
                    and all(
                        row[key] == base[key]
                        for key in ("symbol", "timeframe", "factor_set", "exit_set", "seed")
                    )
                ),
                None,
            )
            if stress is None or claimed_ms < frozen["holdout"]["end_ms"]:
                raise ValueError("final holdout window unavailable")
            request = {
                "family_id": family_id,
                "claim_id": claim_id,
                "dataset_sha256": frozen["dataset"]["sha256"],
                "dataset_start_ms": frozen["dataset"]["start_ms"],
                "dataset_end_ms": frozen["dataset"]["end_ms"],
                "holdout_start_ms": frozen["holdout"]["start_ms"],
                "holdout_end_ms": frozen["holdout"]["end_ms"],
                "family_sha256": family["family_sha256"],
                "identities": frozen["identities"],
                "base_config_id": base_config_id,
                "stress_config_id": stress["config_id"],
                "base_attempt_id": base_attempt_id,
                "stress_attempt_id": stress_attempt_id,
                "producer_id": producer_id,
                "claimed_ms": claimed_ms,
                "state": "consumed",
            }
            if old:
                prior = old["claim"]
                immutable = {key: value for key, value in request.items() if key != "claimed_ms"}
                existing = {key: value for key, value in prior.items() if key != "claimed_ms"}
                if existing != immutable or prior["claimed_ms"] != claimed_ms:
                    raise ValueError("holdout already consumed by different claim")
                receipt = {
                    "claim": prior,
                    "state": old["state"],
                    "events": old["events"],
                    "result": old["result"],
                    "execution_ticket": False,
                    "committed": True,
                }
            else:
                for attempt_id, config_id in (
                    (base_attempt_id, base_config_id),
                    (stress_attempt_id, stress["config_id"]),
                ):
                    value = {
                        "attempt_id": attempt_id,
                        "family_id": family_id,
                        "claim_id": claim_id,
                        "config_id": config_id,
                        "producer_id": producer_id,
                        "registered_ms": claimed_ms,
                        "phase": "final_holdout",
                        "family_sha256": family["family_sha256"],
                        "identities": frozen["identities"],
                    }
                    self._insert_attempt(conn, value)
                    self._attempt_event(
                        conn, attempt_id, "started", attempt_id + ":started", claimed_ms
                    )
                conn.execute(
                    "INSERT INTO refactor_research_holdout_claims(account_id,family_id,claim_id,dataset_sha256,"
                    "holdout_start_ms,holdout_end_ms,payload_sha256,payload) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        self.account_id,
                        family_id,
                        claim_id,
                        request["dataset_sha256"],
                        request["holdout_start_ms"],
                        request["holdout_end_ms"],
                        _hash(request),
                        self.store._json(request),
                    ),
                )
                receipt = {
                    "claim": request,
                    "state": "consumed",
                    "events": [],
                    "result": None,
                    "execution_ticket": True,
                    "committed": True,
                }
        # Ticket is returned only after the connection context committed successfully.
        return receipt

    def record_holdout_outcome(self, claim_id, event_type, *, event_id, observed_ms, error_code):
        claim_id, event_id = _id(claim_id), _id(event_id)
        observed_ms = _time(observed_ms)
        if (
            event_type not in {"unknown", "failed"}
            or not isinstance(error_code, str)
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", error_code)
        ):
            raise ValueError("holdout outcome must be safe unknown/failure evidence")
        value = {
            "claim_id": claim_id,
            "event_id": event_id,
            "event_type": event_type,
            "observed_ms": observed_ms,
            "error_code": error_code,
        }
        with self.store._connection() as conn:
            self._lock(conn)
            query = conn.execute(
                "SELECT family_id FROM refactor_research_holdout_claims WHERE account_id=%s AND claim_id=%s",
                (self.account_id, claim_id),
            ).fetchone()
            if query is None:
                raise ValueError("consumed holdout claim required")
            current = self._claim(conn, query["family_id"])
            old = conn.execute(
                "SELECT * FROM refactor_research_holdout_events WHERE account_id=%s AND event_id=%s",
                (self.account_id, event_id),
            ).fetchone()
            if old:
                if self._verified(old) != value:
                    raise ValueError("immutable holdout event conflict")
                return value
            if current["state"] in HOLDOUT_CLOSED:
                raise ValueError("holdout outcome is already terminal")
            if observed_ms < current["claim"]["claimed_ms"] or (
                current["events"] and observed_ms < current["events"][-1]["observed_ms"]
            ):
                raise ValueError("holdout event timestamp moved backwards")
            conn.execute(
                "INSERT INTO refactor_research_holdout_events(account_id,claim_id,event_id,event_type,observed_ms,payload_sha256,payload) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (
                    self.account_id,
                    claim_id,
                    event_id,
                    event_type,
                    observed_ms,
                    _hash(value),
                    self.store._json(value),
                ),
            )
        return value

    def _attempt_for_claim(self, conn, claim, attempt_id, expected_config):
        attempt = self._attempt(conn, attempt_id)
        if (
            attempt["claim_id"] != claim["claim_id"]
            or attempt["family_id"] != claim["family_id"]
            or attempt["phase"] != "final_holdout"
            or attempt["config_id"] != expected_config
            or attempt["family_sha256"] != claim["family_sha256"]
            or attempt["identities"] != claim["identities"]
            or attempt["producer_id"] != claim["producer_id"]
            or attempt["status"] != "completed"
        ):
            raise ValueError("candidate result requires matching completed claim attempts")
        return attempt

    def record_candidate_result(
        self, claim_id, facts, *, base_artifact_id, stress_artifact_id, observed_ms
    ):
        claim_id, base_artifact_id, stress_artifact_id = map(
            _id, (claim_id, base_artifact_id, stress_artifact_id)
        )
        observed_ms = _time(observed_ms)
        facts = _clean(facts)
        with self.store._connection() as conn:
            self._lock(conn)
            row = conn.execute(
                "SELECT * FROM refactor_research_holdout_claims WHERE account_id=%s AND claim_id=%s",
                (self.account_id, claim_id),
            ).fetchone()
            claim = self._verified(row)
            if claim is None:
                raise ValueError("committed holdout claim required")
            old = conn.execute(
                "SELECT * FROM refactor_research_candidate_results WHERE account_id=%s AND claim_id=%s",
                (self.account_id, claim_id),
            ).fetchone()
            if old:
                previous = self._verified(old)
                supplied = {
                    "claim_id": claim_id,
                    "facts": facts,
                    "base_artifact_id": base_artifact_id,
                    "stress_artifact_id": stress_artifact_id,
                    "observed_ms": observed_ms,
                }
                recorded = {
                    "claim_id": previous["claim_id"],
                    "facts": previous["facts"],
                    "base_artifact_id": previous["base_artifact_id"],
                    "stress_artifact_id": previous["stress_artifact_id"],
                    "observed_ms": previous["observed_ms"],
                }
                if recorded != supplied:
                    raise ValueError("immutable candidate result conflict")
                return previous["public"]
            state = self._claim(conn, claim["family_id"])
            if state["state"] in HOLDOUT_RESULT_BLOCKED:
                raise ValueError("candidate result cannot follow terminal holdout outcome")
            family = self._family(conn, claim["family_id"])
            base = self._attempt_for_claim(
                conn, claim, claim["base_attempt_id"], claim["base_config_id"]
            )
            stress = self._attempt_for_claim(
                conn, claim, claim["stress_attempt_id"], claim["stress_config_id"]
            )
            if observed_ms < max(base["last_ms"], stress["last_ms"], claim["claimed_ms"]):
                raise ValueError("candidate result timestamp precedes attempts")
            base_row = conn.execute(
                "SELECT * FROM refactor_research_artifacts WHERE account_id=%s AND artifact_id=%s",
                (self.account_id, base_artifact_id),
            ).fetchone()
            stress_row = conn.execute(
                "SELECT * FROM refactor_research_artifacts WHERE account_id=%s AND artifact_id=%s",
                (self.account_id, stress_artifact_id),
            ).fetchone()
            base_artifact, stress_artifact = self._verified(base_row), self._verified(stress_row)
            if (
                base_artifact is None
                or stress_artifact is None
                or base_artifact["attempt_id"] != base["attempt_id"]
                or stress_artifact["attempt_id"] != stress["attempt_id"]
            ):
                raise ValueError("candidate artifacts must belong to claimed attempts")
            for artifact in (base_artifact, stress_artifact):
                if (
                    artifact["created_ms"] < claim["claimed_ms"]
                    or artifact["created_ms"] > observed_ms
                ):
                    raise ValueError("candidate artifact time outside claim")
            if (
                facts.get("family_sha256") != claim["family_sha256"]
                or facts.get("config_id") != claim["base_config_id"]
                or facts.get("identities") != claim["identities"]
                or not isinstance(facts.get("stress"), dict)
                or facts["stress"].get("config_id") != claim["stress_config_id"]
            ):
                raise ValueError("candidate identity/configuration mismatch")
            frozen = FrozenResearchEvidence(_canonical(family["frozen"]), family["family_sha256"])
            verdict = evaluate_candidate(facts, frozen).to_dict()
            public = {
                "claim_id": claim_id,
                "verdict": verdict,
                "production_authorized": False,
                "acceptance_eligible": False,
                "source_authenticity_verified": False,
                "base_artifact_id": base_artifact_id,
                "stress_artifact_id": stress_artifact_id,
                "observed_ms": observed_ms,
            }
            record = {
                "claim_id": claim_id,
                "facts": facts,
                "base_artifact_id": base_artifact_id,
                "stress_artifact_id": stress_artifact_id,
                "observed_ms": observed_ms,
                "public": public,
            }
            conn.execute(
                "INSERT INTO refactor_research_candidate_results(account_id,claim_id,payload_sha256,payload) "
                "VALUES(%s,%s,%s,%s)",
                (self.account_id, claim_id, _hash(record), self.store._json(record)),
            )
            event_id = claim_id + ":completed"
            event = {
                "claim_id": claim_id,
                "event_id": event_id,
                "event_type": "completed",
                "observed_ms": observed_ms,
                "result_sha256": _hash(record),
            }
            conn.execute(
                "INSERT INTO refactor_research_holdout_events(account_id,claim_id,event_id,event_type,observed_ms,payload_sha256,payload) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (
                    self.account_id,
                    claim_id,
                    event_id,
                    "completed",
                    observed_ms,
                    _hash(event),
                    self.store._json(event),
                ),
            )
        return public
