"""Content identities for the CarveMe build cache (GM-3/M3)."""
import hashlib
import json
import os
import tempfile

CACHE_SCHEMA = "gem.carveme-cache.v2"


def file_sha256(path):
    if not path:
        return None
    try:
        h = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                h.update(chunk)
        return "sha256:" + h.hexdigest()
    except OSError:
        return None


def _known(value):
    return isinstance(value, str) and bool(value.strip()) and value.strip().lower() not in {"unknown", "none", "n/a"}


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def build_cache_identity(input_path, engine, engine_version, *, diamond_version=None,
                         database_paths=None, params=None, config_path=None,
                         media_table_path=None, engine_path=None, diamond_path=None):
    """Return a verifiable payload/fingerprint, or None if any required identity is unknown.

    Database paths are the actual CarveMe resources selected by its installed config.
    Paths are omitted from the fingerprint because content, not installation location,
    identifies the computation. The caller can record locations separately.
    """
    if not all(_known(x) for x in (engine, engine_version, diamond_version)):
        return None
    if not isinstance(database_paths, dict) or not database_paths:
        return None
    paths = {"input": input_path, "config": config_path, "media_table": media_table_path,
             "engine_binary": engine_path, "diamond_binary": diamond_path}
    paths.update({f"database:{name}": p for name, p in database_paths.items()})
    hashes = {name: file_sha256(p) for name, p in paths.items()}
    if any(value is None for value in hashes.values()):
        return None
    payload = {
        "cache_schema": CACHE_SCHEMA,
        "engine": engine,
        "engine_version": engine_version,
        "diamond_version": diamond_version,
        "input_sha256": hashes.pop("input"),
        "config_sha256": hashes.pop("config"),
        "media_table_sha256": hashes.pop("media_table"),
        "engine_binary_sha256": hashes.pop("engine_binary"),
        "diamond_binary_sha256": hashes.pop("diamond_binary"),
        "database_sha256": {k.removeprefix("database:"): v for k, v in sorted(hashes.items())},
        "params": params or {},
    }
    try:
        fp = "sha256:" + hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()
    except (TypeError, ValueError):
        return None
    return {"cache_schema": CACHE_SCHEMA, "fingerprint": fp, "payload": payload}


def build_cache_fingerprint(input_path, engine, engine_version, params=None,
                            media_table_path=None, config=None, **kwargs):
    """Compatibility wrapper. A fingerprint requires all build identities."""
    identity = build_cache_identity(input_path, engine, engine_version,
                                    params=params, media_table_path=media_table_path,
                                    **kwargs)
    return identity["fingerprint"] if identity else None


def read_fingerprint_file(fingerprint_path):
    try:
        with open(fingerprint_path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def cache_hit(fingerprint_path, expected_identity, output_path):
    """A hit needs schema, canonical payload, expected fingerprint and saved output hash."""
    if not isinstance(expected_identity, dict):
        return {"hit": False, "reason": "identity_unresolved"}
    saved = read_fingerprint_file(fingerprint_path)
    if not saved:
        return {"hit": False, "reason": "sidecar_missing_or_invalid"}
    if saved.get("cache_schema") != CACHE_SCHEMA:
        return {"hit": False, "reason": "schema_mismatch"}
    if saved.get("payload") != expected_identity.get("payload") or saved.get("fingerprint") != expected_identity.get("fingerprint"):
        return {"hit": False, "reason": "fingerprint_mismatch"}
    digest = file_sha256(output_path)
    if not digest:
        return {"hit": False, "reason": "output_missing"}
    if not saved.get("output_sha256") or saved["output_sha256"] != digest:
        return {"hit": False, "reason": "output_hash_mismatch"}
    return {"hit": True, "output_sha256": digest, "fingerprint": saved["fingerprint"]}


def write_cache_record(fingerprint_path, identity, output_path):
    """Write a complete sidecar atomically after the output and validation exist."""
    if not identity or identity.get("cache_schema") != CACHE_SCHEMA:
        raise ValueError("build identity unresolved")
    digest = file_sha256(output_path)
    if not digest:
        raise ValueError("build output missing")
    record = {**identity, "output_sha256": digest}
    parent = os.path.dirname(os.path.abspath(fingerprint_path))
    os.makedirs(parent, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=parent,
                                         prefix=".gem-cache-", suffix=".tmp", delete=False) as handle:
            temp_path = handle.name
            handle.write(_canonical(record) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, fingerprint_path)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)
    return record
