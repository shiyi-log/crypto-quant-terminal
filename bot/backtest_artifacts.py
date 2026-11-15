"""Frozen inputs and validated result artifacts for one research backtest."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib.metadata
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shutil
from typing import Any
import zipfile


def digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def json_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def merge(base: dict, override: dict) -> dict:
    result = deepcopy(base)
    for key, value in override.items():
        result[key] = (merge(result[key], value) if isinstance(value, dict)
                       and isinstance(result.get(key), dict) else deepcopy(value))
    return result


def load_config(path: Path, depth: int = 0) -> dict:
    """Match Freqtrade include precedence; environment is frozen separately."""
    if depth > 5:
        raise ValueError("config_include_loop")
    config = json.loads(path.read_text(encoding="utf-8"))
    inherited = {}
    for include in config.get("add_config_files", []):
        inherited = merge(inherited, load_config(path.parent / include, depth + 1))
    config.pop("add_config_files", None)
    return merge(inherited, config)


def environment_config() -> dict:
    result = {}
    for name, raw in sorted(os.environ.items()):
        if not name.startswith("FREQTRADE__"):
            continue
        parts = name[len("FREQTRADE__"):].split("__")
        value: Any = raw
        if parts[-1] not in {"PASSWORD", "CHAT_ID"}:
            try:
                value = int(raw)
            except ValueError:
                try:
                    value = float(raw)
                except ValueError:
                    if raw.lower() in {"t", "true", "f", "false"}:
                        value = raw.lower() in {"t", "true"}
                    else:
                        try:
                            parsed = json.loads(raw)
                            value = parsed if isinstance(parsed, list) else raw
                        except ValueError:
                            pass
        nested: Any = value
        for index, part in enumerate(reversed(parts)):
            preserve = parts[0].lower() == "exchange" and any(
                p.lower() in {"ccxt_config", "ccxt_sync_config", "ccxt_async_config"}
                for p in parts) and index == 0
            nested = {part if preserve else part.lower(): nested}
        result = merge(result, nested)
    return result


def safe_component(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", value) or value in {".", ".."}:
        raise ValueError("invalid_run_identity")
    return value


def file_inventory(root: Path, suffixes: set[str] | None = None) -> dict:
    records = []
    if root.is_dir():
        for path in sorted(root.rglob("*")):
            if path.is_file() and (suffixes is None or path.suffix in suffixes):
                if any(part in {"__pycache__", ".git"} for part in path.relative_to(root).parts):
                    continue
                before = path.stat()
                sha = digest(path)
                after = path.stat()
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise ValueError("inputs_changed_during_fingerprint")
                records.append({"name": str(path.relative_to(root)), "size": after.st_size,
                                "sha256": sha})
    return {"sha256": json_digest(records), "files": records}


def input_identity(config: dict, strategy: str, model: str, timerange: str,
                   identifier: str, strategy_path: Path, datadir: Path) -> tuple[dict, dict]:
    data = file_inventory(datadir)
    spec = importlib.util.find_spec("freqtrade")
    engine_root = Path(spec.origin).parent if spec and spec.origin else Path("/nonexistent")
    userdir = Path(config["user_data_dir"])
    roots = {"strategy": strategy_path, "user_strategy": userdir / "strategies",
             "custom_model": userdir / "freqaimodels",
             "engine": engine_root, "wrapper": Path(__file__).parent}
    if config.get("freqaimodel_path"):
        roots["extra_model"] = Path(config["freqaimodel_path"]).expanduser().resolve()
    sources = {}
    for name, root in roots.items():
        if name == "wrapper":
            records = [{"name": path.name, "sha256": digest(path)}
                       for path in sorted(root.glob("*.py"))]
            project = root.parent
            for directory in [project, project / "core"]:
                files = directory.glob("*.py") if directory == project else directory.rglob("*.py")
                records.extend({"name": str(path.relative_to(project)), "sha256": digest(path)}
                               for path in sorted(files))
            sources[name] = {"sha256": json_digest(records), "files": records}
        else:
            # Installed UI progress JSON is mutable runtime state, not engine source.
            sources[name] = file_inventory(root, {".py"} if name == "engine" else {".py", ".json"})
    packages = sorted((d.metadata.get("Name", ""), d.version)
                      for d in importlib.metadata.distributions())
    identity = {
        "strategy": strategy, "model": model, "timerange": timerange,
        "identifier": identifier, "config_sha256": json_digest(config),
        "data_sha256": data["sha256"],
        "source_sha256": json_digest({key: item["sha256"] for key, item in sources.items()}),
        "environment_sha256": json_digest(packages),
        "cache_reusable": bool(data["files"] and sources["strategy"]["files"]),
    }
    return identity, {"data": data["files"], "sources": sources, "packages": packages}


def read_stats(path: Path, strategy: str | None = None, timerange: str | None = None) -> tuple[str, dict]:
    with zipfile.ZipFile(path) as archive:
        candidates = []
        for name in archive.namelist():
            if name.endswith(".json"):
                data = json.loads(archive.read(name))
                if isinstance(data, dict) and isinstance(data.get("strategy"), dict) and data["strategy"]:
                    candidates.append(data["strategy"])
        if len(candidates) != 1:
            raise ValueError("ambiguous_or_missing_statistics")
    strategies = candidates[0]
    if len(strategies) != 1 or (strategy is not None and strategy not in strategies):
        raise ValueError("result_strategy_mismatch")
    name, stats = next(iter(strategies.items()))
    if timerange is not None and stats.get("timerange") != timerange:
        raise ValueError("result_timerange_mismatch")
    def finite(value: Any) -> None:
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("nonfinite_result")
        if isinstance(value, dict):
            for item in value.values():
                finite(item)
        if isinstance(value, list):
            for item in value:
                finite(item)
    finite(stats)
    if type(stats.get("total_trades")) is not int or stats["total_trades"] < 0:
        raise ValueError("invalid_trade_count")
    return name, stats


def reuse_result(prior: Path, identity: dict, destination: Path) -> tuple[Path | None, str | None]:
    try:
        manifest = json.loads((prior / "manifest.json").read_text())
        if manifest.get("schema") != "quant-backtest-run-v1":
            return None, "cache_schema_mismatch"
        if not identity["cache_reusable"] or manifest["identity"] != identity:
            return None, "cache_identity_mismatch"
        if manifest.get("execution_status") != "succeeded":
            return None, "cache_not_successful"
        for name, key in [("config.json", "config_sha256"), ("inputs.json", "inputs_sha256")]:
            if digest(prior / name) != manifest[key]:
                return None, "cache_input_evidence_hash_mismatch"
        execution_name = manifest.get("execution_config_file", "config.json")
        execution_config = prior / execution_name
        if execution_config.parent.resolve() != prior.resolve():
            return None, "cache_execution_config_invalid"
        execution_hash = manifest.get("execution_config_sha256", manifest["config_sha256"])
        if digest(execution_config) != execution_hash:
            return None, "cache_execution_config_hash_mismatch"
        source = prior / manifest["result_file"]
        if source.parent.resolve() != prior.resolve() or digest(source) != manifest["result_sha256"]:
            return None, "cache_result_hash_mismatch"
        read_stats(source, identity["strategy"], identity["timerange"])
        result = destination / "cached-result.zip"
        shutil.copy2(execution_config, destination / "execution_config.json")
        shutil.copy2(source, result)
        if digest(result) != manifest["result_sha256"]:
            raise ValueError("cache_copy_hash_mismatch")
        return result, None
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile):
        (destination / "cached-result.zip").unlink(missing_ok=True)
        (destination / "execution_config.json").unlink(missing_ok=True)
        return None, "cache_unverifiable"
