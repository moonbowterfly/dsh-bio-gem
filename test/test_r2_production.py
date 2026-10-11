"""Production wiring for M1-M4 with isolated engine/solver fixtures."""
import csv
import json
import os
import pathlib
import shutil
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))
import cobra
import bootstrap_carveme
import build
import coherence
import double_knockout
import essential_scan
import fingerprint
import gapfind
import gem_ops
import ledger
import model_card
import phenotype_fix
import secretion
import validate


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    print("PASS", name)


def toy_sbml(path):
    model = cobra.Model("gem-r2-toy")
    e = cobra.Metabolite("glc_e", compartment="e", formula="C6H12O6")
    c = cobra.Metabolite("glc_c", compartment="c", formula="C6H12O6")
    ex = cobra.Reaction("EX_glc_e", lower_bound=-10, upper_bound=1000)
    transport = cobra.Reaction("T", lower_bound=0, upper_bound=1000)
    biomass = cobra.Reaction("BIOMASS", lower_bound=0, upper_bound=1000)
    model.add_reactions([ex, transport, biomass])
    ex.add_metabolites({e: -1})
    transport.add_metabolites({e: -1, c: 1})
    biomass.add_metabolites({c: -1})
    biomass.gene_reaction_rule = "g1 or g2"
    model.objective = biomass
    cobra.io.write_sbml_model(model, str(path))


def fake_report(*_, **kwargs):
    g3 = {"status": "PASS", "growth_medium": 1.0}
    g4 = {"status": "SKIP", "reason": "no phenotype reference"}
    if kwargs.get("phenotype_table"):
        g4 = {"status": "PASS", "matched": 1, "total": 1,
              "results": [{"substrate": "Glucose", "published": 1, "predicted": 1,
                           "growth": 1.0, "exchange": "EX_glc_e", "match": True,
                           "applied_medium_bounds": {"EX_glc_e": [-10.0, 1000.0]}}]}
    return {"overall": "PASS", "g0": {"status": "PASS"}, "g1": {"status": "PASS"},
            "g2": {"status": "PASS"}, "g3": g3, "g4": g4, "g6": {"status": "PASS"}}


with tempfile.TemporaryDirectory(prefix="gem-r2-production-") as temp:
    root = pathlib.Path(temp)
    ledger.LEDGER_DIR = str(root / "ledgers")
    ledger.LEDGER_PATH = str(root / "legacy-global.jsonl")
    pathlib.Path(ledger.LEDGER_DIR).mkdir()
    source = root / "toy.xml"
    protein = root / "protein.faa"
    toy_sbml(source)
    protein.write_bytes(b">protein\nMKT\n")
    venv = root / "venv"
    package = venv / "Lib" / "site-packages" / "carveme"
    data = package / "data"
    (venv / "Scripts").mkdir(parents=True)
    (data / "input").mkdir(parents=True)
    (data / "generated").mkdir(parents=True)
    (venv / "Scripts" / "carve.exe").write_bytes(b"engine-v1")
    (venv / "Scripts" / "diamond.exe").write_bytes(b"diamond-v1")
    (package / "config.cfg").write_text(
        "[input]\nmedia_library=data/input/media_db.tsv\n[generated]\n"
        "diamond_db=data/generated/bigg_proteins.dmnd\n"
        "default_universe=data/generated/universe_bacteria.xml.gz\n"
        "gene_annotations=data/generated/gene_annotations.tsv.gz\n"
        "bigg_gprs=data/generated/bigg_gprs.csv.gz\n", encoding="utf-8")
    (data / "input" / "media_db.tsv").write_text("medium\tcompound\nM9\tglc\n", encoding="utf-8")
    for filename in ("bigg_proteins.dmnd", "universe_bacteria.xml.gz",
                     "gene_annotations.tsv.gz", "bigg_gprs.csv.gz"):
        (data / "generated" / filename).write_bytes(filename.encode())
    calls = []

    def fake_carve(_protein, output, **kwargs):
        calls.append({"protein": _protein, "output": output})
        shutil.copyfile(source, output)
        return output

    output_dir = root / "models"
    progress = root / "build.progress.jsonl"
    with patch.object(bootstrap_carveme, "ensure_carveme", return_value={"ready": True, "action": "already"}), \
         patch.object(build, "_carve_version", return_value="1.6.0"), \
         patch.object(build, "_diamond_version", return_value="2.2.8"), \
         patch.object(build, "run_carve", side_effect=fake_carve), \
         patch("validate.validate_model", side_effect=fake_report):
        def run(**kwargs):
            return build.build(str(protein), name="run", venv=str(venv), out_dir=str(output_dir),
                               progress_path=str(progress), **kwargs)

        first = run()
        check("M3 first production build invokes external engine", len(calls) == 1)
        sidecar = pathlib.Path(first["model"] + ".fingerprint.json")
        record = json.loads(sidecar.read_text(encoding="utf-8"))
        check("M3 production sidecar has schema, payload and output hash",
              record["cache_schema"] == fingerprint.CACHE_SCHEMA
              and record["output_sha256"] == fingerprint.file_sha256(first["model"])
              and record["payload"]["database_sha256"]["diamond_db"])
        second = run()
        check("M3 same inputs hit production cache", len(calls) == 1)
        target = run(medium={"EX_glc_e": -5.0})
        check("M3 target medium change revalidates without rebuilding", len(calls) == 1
              and target["target"]["g3"] == "PASS")
        card = model_card.load_card(first["model"])
        m9 = next(x for x in card["supported_mediums"] if x["medium_name"] == "M9")
        check("M2 build G3-only effective evidence wired", m9["validation_status"] == "verified_G3_only"
              and m9["effective_validation_status"] == "verified_G3_only")
        fixed_mtime = protein.stat().st_mtime
        protein.write_bytes(b">protein\nMKT2\n")
        os.utime(protein, (fixed_mtime, fixed_mtime))
        run()
        check("M3 input changed with preserved mtime rebuilds", len(calls) == 2)
        # Keep valid SBML so a bad cache hit reaches the intended engine-count
        # assertion instead of failing early in the XML parser.
        pathlib.Path(first["model"]).write_bytes(
            pathlib.Path(first["model"]).read_bytes() + b"\n<!-- tampered but valid SBML -->")
        run()
        check("M3 output tamper rebuilds", len(calls) == 3)
        # Every file that the installed CarveMe command actually reads must
        # invalidate the same production branch, not only a helper hash.
        for changed in (package / "config.cfg", data / "input" / "media_db.tsv",
                        venv / "Scripts" / "carve.exe", venv / "Scripts" / "diamond.exe",
                        *(data / "generated" / filename for filename in
                          ("bigg_proteins.dmnd", "universe_bacteria.xml.gz",
                           "gene_annotations.tsv.gz", "bigg_gprs.csv.gz"))):
            before_calls = len(calls)
            changed.write_bytes(changed.read_bytes() +
                                (b"\n# changed" if changed.name == "config.cfg" else b"\nchanged"))
            run()
            check("M3 dependency identity rebuilds: " + changed.name, len(calls) == before_calls + 1)
        # Version strings and actual executable bytes are separate identities.
        before_calls = len(calls)
        with patch.object(build, "_carve_version", return_value="1.6.1"):
            run()
        check("M3 CarveMe version change rebuilds", len(calls) == before_calls + 1)
        before_calls = len(calls)
        with patch.object(build, "_diamond_version", return_value="2.2.9"):
            run()
        check("M3 DIAMOND version change rebuilds", len(calls) == before_calls + 1)
        # No sidecar, a malformed sidecar and missing output are all cache misses.
        sidecar.unlink()
        before_calls = len(calls)
        run()
        check("M3 old output without sidecar rebuilds", len(calls) == before_calls + 1)
        sidecar.write_text("{bad json", encoding="utf-8")
        before_calls = len(calls)
        run()
        check("M3 corrupt sidecar rebuilds", len(calls) == before_calls + 1)
        pathlib.Path(first["model"]).unlink()
        before_calls = len(calls)
        run()
        check("M3 missing output rebuilds", len(calls) == before_calls + 1)
        protein.write_bytes(b">protein\nMKT3\n")
        with patch.object(build, "_carve_version", return_value="unknown"):
            before_calls = len(calls)
            run()
            run()
        check("M3 unknown engine version never hits", len(calls) == before_calls + 2)
        with patch.object(build, "_diamond_version", return_value="unknown"):
            before_calls = len(calls)
            run()
            run()
        check("M3 unknown DIAMOND version never hits", len(calls) == before_calls + 2)
        protein.write_bytes(b">protein\nMKT4\n")
        with patch("fingerprint.write_cache_record", side_effect=OSError("injected sidecar failure")):
            before_calls = len(calls)
            try:
                run()
            except OSError:
                pass
            else:
                raise AssertionError("M3 sidecar write failure was swallowed")
        check("M3 sidecar write failure invokes engine and fails", len(calls) == before_calls + 1)
        protein.write_bytes(b">protein\nMKT5\n")
        old_model_hash = fingerprint.file_sha256(first["model"])
        old_sidecar_hash = fingerprint.file_sha256(sidecar)
        with patch.object(build, "run_carve", side_effect=RuntimeError("injected backend failure")):
            try:
                run()
            except RuntimeError as error:
                check("M3 backend exception is surfaced", "injected backend failure" in str(error))
            else:
                raise AssertionError("M3 backend exception was swallowed")
        check("M3 backend failure keeps prior output and sidecar", fingerprint.file_sha256(first["model"]) == old_model_hash
              and fingerprint.file_sha256(sidecar) == old_sidecar_hash)

    # The product report consumes the effective claim, not the stored assertion.
    report = gem_ops.op_model_info({"model": first["model"]})
    check("M2 gem_report shows audited effective card status", report["ok"]
          and any(x["effective"] == "verified_G3_only" for x in report["result"]["effective_validation"]))
    phenotype_table = root / "phenotype.tsv"
    phenotype_table.write_text("substrate\tpublished\nGlucose\t1\n", encoding="utf-8")
    phenotype_ledger = root / "ledgers" / "phenotype.jsonl"
    with patch.object(phenotype_fix, "validate_model", side_effect=fake_report):
        phenotype = phenotype_fix.phenotype_fix(first["model"], phenotype_table=str(phenotype_table),
                                               medium={"EX_glc_e": -10.0},
                                               ledger_path=str(phenotype_ledger))
    check("M2 phenotype_fix actual writer records reference bytes", phenotype.get("card_version")
          and phenotype.get("ledger_registration", {}).get("appended") == 1)
    written = model_card.load_card(first["model"])
    sole = next(x for x in written["supported_mediums"] if x["medium_name"] == "custom/sole")
    check("M2 G3+G4 only on actual sole condition", sole["effective_validation_status"] == "verified_G3_G4"
          and next(x for x in written["supported_mediums"] if x["medium_name"] == "M9")["effective_validation_status"] == "verified_G3_only")
    legacy_model = root / "legacy.xml"
    shutil.copyfile(first["model"], legacy_model)
    legacy_card = model_card.card_path_for(str(legacy_model))
    pathlib.Path(legacy_card).write_text(json.dumps({"supported_mediums": [
        {"medium_name": "M9", "validation_status": "verified_G3_G4"}]}), encoding="utf-8")
    legacy_bytes = pathlib.Path(legacy_card).read_bytes()
    legacy_report = gem_ops.op_model_info({"model": str(legacy_model)})
    check("M2 legacy card consumer cannot endorse old G3+G4", legacy_report["ok"]
          and legacy_report["result"]["effective_validation"][0]["effective"] == "legacy_claim_unconfirmed"
          and pathlib.Path(legacy_card).read_bytes() == legacy_bytes)
    for label, alter in (
            ("wrong-model", lambda target, card: target.write_bytes(target.read_bytes() + b"\n<!--new snapshot-->")),
            ("wrong-medium", lambda target, card: card["supported_mediums"][1].update(
                {"medium_identity": "sha256:" + "0" * 64})),
            ("g4-fail", lambda target, card: card["supported_mediums"][1]["validation_evidence"]["g4"].update(
                {"status": "FAIL"})),
    ):
        model_copy = root / f"audit-{label}.xml"
        shutil.copyfile(first["model"], model_copy)
        card_copy = model_card.card_path_for(str(model_copy))
        raw_card = json.loads(pathlib.Path(model_card.card_path_for(first["model"])).read_text(encoding="utf-8"))
        alter(model_copy, raw_card)
        pathlib.Path(card_copy).write_text(json.dumps(raw_card), encoding="utf-8")
        observed = model_card.load_card(str(model_copy))
        check("M2 card audit rejects " + label,
              all(m["effective_validation_status"] != "verified_G3_G4"
                  for m in observed["supported_mediums"] if m["medium_name"] == "custom/sole"))
    phenotype_rows, _ = ledger.load_rows(str(phenotype_ledger))
    check("M4 phenotype producer stores real context/subject", phenotype_rows[0]["subject"]["substrate"] == "Glucose"
          and phenotype_rows[0]["analysis_context"]["model_sha256"] == ledger.sbml_sha256(first["model"])
          and phenotype_rows[0]["analysis_context"]["applied_medium_bounds"]["EX_glc_e"][0] == -10)

    with patch.object(essential_scan, "scan_essentiality", side_effect=lambda *_, **__: {
        "wt_growth": 1.0, "essential_genes": ["g1"], "tested_genes": 2}):
        ess = essential_scan.essential_scan(first["model"], medium={"EX_glc_e": -10.0},
                                           ledger_path=str(root / "ledgers" / "essential.jsonl"))
    ess_rows, _ = ledger.load_rows(str(root / "ledgers" / "essential.jsonl"))
    check("M4 essential_scan production context is resolved", ess["ledger_registration"]["appended"] == 1
          and ess_rows[0]["identity_status"] == "resolved"
          and ess_rows[0]["subject"] == {"gene": "g1"})
    with patch.object(essential_scan, "scan_essentiality", side_effect=lambda *_, **__: {
        "wt_growth": 1.0, "essential_genes": ["g1"], "tested_genes": 2}):
        changed_bounds = essential_scan.essential_scan(
            first["model"], medium={"EX_glc_e": -20.0},
            ledger_path=str(root / "ledgers" / "essential.jsonl"))
        same_bytes = root / "same-bytes" / "run.xml"
        same_bytes.parent.mkdir()
        shutil.copyfile(first["model"], same_bytes)
        shutil.copyfile(model_card.card_path_for(first["model"]),
                        model_card.card_path_for(str(same_bytes)))
        same_content = essential_scan.essential_scan(
            str(same_bytes), medium={"EX_glc_e": -10.0},
            ledger_path=str(root / "ledgers" / "essential.jsonl"))
    check("M4 producer same label different bounds yields new UID",
          changed_bounds["ledger_registration"]["appended"] == 1)
    check("M4 producer same bytes different path deduplicates",
          same_content["ledger_registration"]["skipped_duplicates"] == 1)

    with patch.object(secretion, "find_biomass_gam", return_value={"biomass_rxn": "BIOMASS"}), \
         patch.object(secretion, "_envelope_for", return_value=([{"fraction": 0.5, "prod": 1.0}], 1.0, 0.5)):
        sec = secretion.secretion(first["model"], medium={"EX_glc_e": -10.0}, fractions=[0.5],
                                 export_csv=str(root / "secretion.csv"),
                                 ledger_path=str(root / "ledgers" / "secretion.jsonl"), mode="summary")
    sec_rows, _ = ledger.load_rows(str(root / "ledgers" / "secretion.jsonl"))
    check("M4 secretion producer context and metabolite subject", bool(sec_rows)
          and sec_rows[0]["analysis_context"]["identity_status"] == "resolved"
          and sec_rows[0]["subject"]["metabolite"])

    fake_scan = {"essential_genes": [], "candidate_genes": ["g1", "g2"],
                 "tested_genes": 2, "active_rxn_ids": ["BIOMASS"]}
    with patch.object(double_knockout, "scan_essentiality", return_value=fake_scan), \
         patch.object(double_knockout, "_gpr_or_pairs", return_value={("g1", "g2"): ["BIOMASS"]}), \
         patch.object(double_knockout, "_double_growth", return_value=0.0), \
         patch.object(double_knockout, "_single_growth", return_value=1.0):
        dk = double_knockout.double_knockout(first["model"], medium={"EX_glc_e": -10.0},
                                            max_pairs=1, ledger_path=str(root / "ledgers" / "dk.jsonl"))
    dk_rows, _ = ledger.load_rows(str(root / "ledgers" / "dk.jsonl"))
    check("M4 double_knockout producer sorts pair subject", dk["ledger_registration"]["appended"] == 1
          and dk_rows[0]["subject"]["genes"] == ["g1", "g2"])

    # Same basename is only a storage convenience. All four consumers must
    # filter an explicit shared ledger by actual model bytes.
    model_a = root / "consumer-a" / "same.xml"
    model_b = root / "consumer-b" / "same.xml"
    model_a.parent.mkdir()
    model_b.parent.mkdir()
    shutil.copyfile(first["model"], model_a)
    model_b.write_bytes(model_a.read_bytes() + b"\n<!-- another model snapshot -->")
    shared_ledger = root / "ledgers" / "shared-consumer.jsonl"
    for path, gene in ((model_a, "gA"), (model_b, "gB")):
        loaded = cobra.io.read_sbml_model(str(path))
        context = ledger.make_analysis_context(str(path), loaded, "producer-probe", "1",
                                               applied_bounds={"EX_glc_e": [-10.0, 1000.0]})
        registered = ledger.register_predictions([{"type": "essentiality", "subject": {"gene": gene},
            "analysis_context": context, "content": f"{gene} display", "model": str(path),
            "condition": "AB", "status": "unverified"}], path=str(shared_ledger))
        check("M4 shared-ledger fixture producer resolved " + gene,
              context["identity_status"] == "resolved" and registered["appended"] == 1)
    for path, gene in ((model_a, "gA"), (model_b, "gB")):
        queried = gem_ops.op_ledger({"action": "query", "model": str(path),
                                     "ledger_path": str(shared_ledger)})
        check("M4 op_ledger content isolates " + gene, queried["ok"]
              and queried["result"]["matched"] == 1
              and queried["result"]["results"][0]["subject"] == {"gene": gene})
        summary = ledger.ledger_summary(model=str(path), path=str(shared_ledger))
        check("M4 ledger_summary content isolates " + gene,
              summary["own_model_entries"] == 1)
        report = gem_ops.op_model_info({"model": str(path), "ledger_path": str(shared_ledger)})
        check("M4 gem_report content isolates " + gene, report["ok"]
              and report["result"]["ledger_summary"]["own_model_entries"] == 1)
        exported = gem_ops.op_targets({"model": str(path), "ledger_path": str(shared_ledger),
                                       "export_path": str(root / f"targets-{gene}.csv")})
        check("M4 gem_targets content isolates " + gene, exported["ok"]
              and exported["result"]["exported_count"] == 1
              and exported["result"]["rows"][0]["genes"] == [gene])

    validator = validate.Validator.__new__(validate.Validator)
    validator.m = object()
    validator.path = str(first["model"])
    validator.g1_load = lambda: {"status": "PASS"}
    validator.g2_balance = lambda: {"status": "PASS"}
    validator.g3_growth = lambda *_: {"status": "PASS"}
    with patch.object(coherence, "model_coherence", lambda _: {"status": "PASS"}), \
         patch.object(gapfind, "expand_medium", lambda _: ({}, None)):
        for g6, expected in (("SKIP", "WARN"), ("ERROR", "FAIL"), ("FAIL", "FAIL"), ("PASS", "PASS")):
            validator.g6_atp_leak = lambda g6=g6, **_: {"status": g6, "reason": "controlled"}
            overall = validator.run()["overall"]
            check(f"M1 production Validator.run G6 {g6} -> {expected}", overall == expected)

print("R2 GEM PRODUCTION ALL PASS")
