"""M3/M4 content identity, non-destructive migration and consumer isolation."""
import csv
import hashlib
import json
import os
import pathlib
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))
import fingerprint as fp
import ledger as lg
import targets as tg


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    print("PASS", name)


def context(model_hash, bounds=-10, method="v1", tolerance=1e-7):
    return {"identity_policy": lg.IDENTITY_POLICY, "model_sha256": model_hash,
            "method": "essentiality", "method_version": method,
            "applied_medium_bounds": {"EX_A": [bounds, 1000]}, "parameters": {"cutoff": 1e-6},
            "reference_sha256": None,
            "solver": {"interface": "optlang.glpk_interface", "version": "5.0.13",
                       "tolerances": {"feasibility": tolerance, "optimality": tolerance}},
            "identity_status": "resolved"}


with tempfile.TemporaryDirectory(prefix="gem-r2-gm3-") as temp:
    root = pathlib.Path(temp)
    lg.LEDGER_DIR = str(root / "ledgers")
    lg.LEDGER_PATH = str(root / "legacy-global.jsonl")
    pathlib.Path(lg.LEDGER_DIR).mkdir()
    a = root / "a" / "same.xml"
    b = root / "b" / "same.xml"
    c = root / "c" / "same.xml"
    for p, value in ((a, b"A"), (b, b"A"), (c, b"B")):
        p.parent.mkdir()
        p.write_bytes(value)
    h_a = lg.sbml_sha256(str(a))
    h_c = lg.sbml_sha256(str(c))
    ledger_path = root / "ledgers" / "shared.jsonl"

    def row(model, prose="display one", bounds=-10, method="v1", tolerance=1e-7, **extra):
        mh = lg.sbml_sha256(str(model))
        return {"type": "essentiality", "subject": {"gene": "g1"},
                "content": prose, "model": str(model), "condition": "AB",
                "analysis_context": context(mh, bounds, method, tolerance), **extra}

    first = lg.register_predictions([row(a)], path=str(ledger_path))
    check("first current prediction written", first["appended"] == 1)
    check("same bytes at another path deduplicate", lg.register_predictions([row(b)], path=str(ledger_path))["skipped_duplicates"] == 1)
    check("display prose change does not change subject UID", lg.register_predictions([row(a, prose="renamed prose")], path=str(ledger_path))["skipped_duplicates"] == 1)
    check("same condition label with new bounds is new", lg.register_predictions([row(a, bounds=-20)], path=str(ledger_path))["appended"] == 1)
    check("method version is new", lg.register_predictions([row(a, method="v2")], path=str(ledger_path))["appended"] == 1)
    check("solver tolerance is new", lg.register_predictions([row(a, tolerance=1e-8)], path=str(ledger_path))["appended"] == 1)
    check("same basename with different bytes is new", lg.register_predictions([row(c)], path=str(ledger_path))["appended"] == 1)
    fake = row(a, method="v3", model_sha256=h_c)
    result = lg.register_predictions([fake], path=str(ledger_path))
    check("caller fake hash not trusted or deduplicated", result["appended"] == 1)
    rows, _ = lg.load_rows(str(ledger_path))
    check("fake hash replaced with actual bytes and flagged", rows[-1]["model_sha256"] == h_a
          and rows[-1]["identity_warning"] == "caller_model_hash_ignored")

    visible = lg.query_ledger(model=str(a), path=str(ledger_path))
    check("explicit ledger still filters model bytes", visible["matched"] == 5
          and all(r["model_sha256"] == h_a for r in visible["results"]))
    summary = lg.ledger_summary(model=str(c), path=str(ledger_path))
    check("summary isolates B of same basename", summary["total"] == 1 and summary["own_model_entries"] == 1)
    output = root / "targets.csv"
    export = tg.targets(model_path=str(c), ledger_path=str(ledger_path), export_path=str(output), progress=lambda _: None)
    with output.open(encoding="utf-8-sig", newline="") as handle:
        exported = list(csv.DictReader(handle))
    meta = json.loads(pathlib.Path(export["meta_path"]).read_text(encoding="utf-8"))
    check("targets fixed 11 columns and content isolation", len(exported) == 1
          and len(exported[0]) == 11 and exported[0]["genes"] == "g1")
    check("targets sidecar binds output/model UID", meta["model_sha256"] == h_c
          and meta["rows"][0]["prediction_uid"] and meta["export_sha256"] == "sha256:" + sha(output))

    historical = root / "historic.jsonl"
    historical.write_bytes((json.dumps({"prediction_id": "P0001", "model": str(a),
                                       "status": "experimentally_verified", "content": "old"}) + "\n").encode()
                           + b"{invalid raw\r\n")
    original_hash = sha(historical)
    dry = lg.migrate_ledger_identity(path=str(historical), dry_run=True)
    check("dry run zero writes, today model remains unresolved", sha(historical) == original_hash
          and dry["per_file"][historical.name]["legacy_unresolved"] == 1)
    migrated = lg.migrate_ledger_identity(path=str(historical), dry_run=False, out_dir=str(root / "migration"))
    detail = migrated["per_file"][historical.name]
    new_rows, _ = lg.load_rows(detail["ledger"])
    manifest = json.loads(pathlib.Path(detail["manifest"]).read_text(encoding="utf-8"))
    check("source unchanged; new old row unresolved", sha(historical) == original_hash
          and new_rows[0]["identity_status"] == "legacy_identity_unresolved"
          and "model_sha256" not in new_rows[0])
    check("bad row exact bytes and line mapped", pathlib.Path(detail["quarantine"]).read_bytes() == b"{invalid raw\r\n"
          and manifest["quarantine"][0]["line"] == 2)
    again = lg.migrate_ledger_identity(path=str(historical), dry_run=False, out_dir=str(root / "migration"))
    check("migration same source and policy idempotent", again["per_file"][historical.name]["idempotent_reuse"])
    failure_dir = root / "failed-migration"
    real_open = open

    def fail_manifest(path, *args, **kwargs):
        if str(path).endswith("manifest.json") and ".gem-migrate-" in str(path):
            raise OSError("injected manifest failure")
        return real_open(path, *args, **kwargs)

    with patch("builtins.open", side_effect=fail_manifest):
        try:
            lg.migrate_ledger_identity(path=str(historical), dry_run=False, out_dir=str(failure_dir))
        except OSError:
            pass
        else:
            raise AssertionError("manifest failure injection survived")
    check("manifest failure leaves source and no finalized output", sha(historical) == original_hash
          and not list(failure_dir.glob("historic.jsonl-*")))

    inp = root / "input.faa"
    out = root / "out.xml"
    inp.write_bytes(b">x\nAAA\n")
    out.write_bytes(b"original")
    deps = {}
    for key in ("config", "media", "carve", "diamond", "database"):
        dep = root / key
        dep.write_bytes(key.encode())
        deps[key] = str(dep)

    def identity():
        return fp.build_cache_identity(str(inp), "carveme", "1.6.0", diamond_version="2.2.8",
                                       database_paths={"default": deps["database"]},
                                       config_path=deps["config"], media_table_path=deps["media"],
                                       engine_path=deps["carve"], diamond_path=deps["diamond"],
                                       params={"gapfill_medium": "M9"})

    sidecar = str(out) + ".fingerprint.json"
    expected = identity()
    fp.write_cache_record(sidecar, expected, str(out))
    check("valid schema/output hash hits", fp.cache_hit(sidecar, expected, str(out))["hit"])
    out.write_bytes(b"tampered")
    check("tampered output misses", fp.cache_hit(sidecar, expected, str(out))["reason"] == "output_hash_mismatch")
    check("unknown engine identity cannot cache", fp.build_cache_identity(str(inp), "carveme", "unknown",
          diamond_version="2.2.8", database_paths={"default": deps["database"]},
          config_path=deps["config"], media_table_path=deps["media"], engine_path=deps["carve"],
          diamond_path=deps["diamond"]) is None)

print("GM3/M3/M4 ALL PASS")
