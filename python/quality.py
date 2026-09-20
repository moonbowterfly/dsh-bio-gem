"""Model-quality report primitives for the ``quality`` gem operation.

The report intentionally stays at the model/constraint level.  It is a quick
comparison aid, not a replacement for model curation or biological validation.
"""
import contextlib
import csv
import hashlib
import io
import os
from collections import defaultdict, deque

from cobra.flux_analysis import fastcc, find_blocked_reactions

from gapfind import expand_medium, resolve_medium
from silentio import silent_read_sbml


EX_PREFIXES = ("EX_", "DM_", "SK_")
ID_SAMPLE_LIMIT = 25
BALANCE_TOLERANCE = 1e-6
CYCLE_DIRECTION_BOUND = 1e4

CHECK_NAMES = (
    "blocked_reactions",
    "cyclic_reactions",
    "elemental_balance",
    "orphan_metabolites",
    "dead_end_metabolites",
    "gpr_coverage",
    "annotation_coverage",
    "connectivity",
)

WEIGHTS = {
    "blocked": 0.2,
    "balance": 0.2,
    "cyclic": 0.1,
    "gpr": 0.25,
    "reaction_annotation": 0.125,
    "metabolite_annotation": 0.125,
}

QUALITY_INDEX_NOTE = (
    "quality_index 是启发式聚合（gem-qi-v1），仅用于快速比较；不得作为单一质量结论引用——"
    "分项指标与 failed_checks 才是判断依据"
)


def _model_hash(path):
    """Return the stable, compact content hash required by the operation API."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()[:16]


def _is_boundary(reaction):
    """Use the repository's boundary/exchange convention consistently."""
    return reaction.boundary or reaction.id.startswith(EX_PREFIXES)


def _internal_reactions(model):
    return [reaction for reaction in model.reactions if not _is_boundary(reaction)]


def _selected_checks(checks):
    if checks is None:
        return list(CHECK_NAMES)
    if not isinstance(checks, list) or any(not isinstance(item, str) for item in checks):
        raise ValueError("checks must be an array of quality check names")

    selected = []
    for item in checks:
        if item not in CHECK_NAMES:
            allowed = ", ".join(CHECK_NAMES)
            raise ValueError(f"unknown quality check: {item} (allowed: {allowed})")
        if item not in selected:
            selected.append(item)
    return selected


def _configure_medium(model, medium):
    """Apply an explicit medium, or preserve the SBML's bounds when omitted."""
    if medium is not None and not isinstance(medium, dict):
        raise ValueError("medium must be an object when provided")

    expanded, preset = expand_medium(medium)
    resolved, unresolved = resolve_medium(model, expanded) if expanded else ({}, [])

    # An omitted medium is explicitly different from an empty supplied medium:
    # the former uses the model's own defaults, while the latter closes uptake.
    if medium is not None:
        for reaction in model.reactions:
            if _is_boundary(reaction):
                reaction.lower_bound = 0.0
        for reaction_id, lower_bound in resolved.items():
            model.reactions.get_by_id(reaction_id).lower_bound = lower_bound

    return {
        "preset": preset,
        "resolved_exchanges": len(resolved),
    }, unresolved


def _blocked_reaction_ids(model):
    """Find blocked internal reactions with the Windows-safe FVA setting."""
    internal = _internal_reactions(model)
    if not internal:
        return [], 0

    # Cobra itself is normally quiet here, but redirecting protects the JSON
    # stdin/stdout protocol from a solver/backend informational message.
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        blocked = find_blocked_reactions(
            model,
            reaction_list=internal,
            processes=1,
        )
    return sorted(str(reaction_id) for reaction_id in blocked), len(internal)


def _cyclic_direction_cone(model):
    """Build cobra's documented cyclic-reaction S+direction problem as a Model.

    ``find_cyclic_reactions`` intentionally ignores medium and other model
    constraints.  A fresh model is therefore required here: copying the source
    model would accidentally retain those constraints.  Reverse-only reactions
    are stoichiometrically flipped so FASTCC can evaluate a non-negative flux
    cone without losing their original identifiers.
    """
    import cobra

    cone = cobra.Model("gem_cyclic_direction_cone")
    metabolites = {}

    def cone_metabolite(metabolite):
        if metabolite.id not in metabolites:
            metabolites[metabolite.id] = cobra.Metabolite(
                metabolite.id,
                name=metabolite.name,
                compartment=metabolite.compartment,
            )
        return metabolites[metabolite.id]

    # Keep every non-boundary column while constructing S.  The final report
    # later applies the repository's EX/DM/SK filtering, but those unusual
    # non-boundary columns must still be allowed to complete an internal cycle.
    for reaction in model.reactions:
        if reaction.boundary:
            continue
        lower_bound, upper_bound = reaction.bounds
        if lower_bound == 0.0 and upper_bound == 0.0:
            continue

        if lower_bound < 0.0 and upper_bound > 0.0:
            sign, bounds = 1.0, (-CYCLE_DIRECTION_BOUND, CYCLE_DIRECTION_BOUND)
        elif upper_bound > 0.0:
            sign, bounds = 1.0, (0.0, CYCLE_DIRECTION_BOUND)
        elif lower_bound < 0.0:
            sign, bounds = -1.0, (0.0, CYCLE_DIRECTION_BOUND)
        else:
            # An invalid or fixed-zero bound cannot contribute to a nonzero
            # direction cone.  The normal cobra model validator rejects it.
            continue

        cone_reaction = cobra.Reaction(reaction.id, name=reaction.name)
        cone_reaction.lower_bound, cone_reaction.upper_bound = bounds
        cone_reaction.add_metabolites({
            cone_metabolite(metabolite): sign * coefficient
            for metabolite, coefficient in reaction.metabolites.items()
        })
        cone.add_reactions([cone_reaction])
    return cone


def _cyclic_reaction_ids(model):
    """Find reactions that can carry flux in the structural steady-state cone.

    On cobra 0.32.1/GLPK, the native randomized optimized loop detector can run
    for a very long time and fail during per-direction verification on large
    models.  FASTCC is a deterministic, cobra-bundled LP consistency algorithm.
    Applied to the fresh S+direction cone above, a retained reaction is exactly
    a nonzero steady-state direction and hence a member of the cyclic-reaction
    union reported by ``find_cyclic_reactions``; it does not use medium bounds
    or source-model solver constraints.
    """
    cone = _cyclic_direction_cone(model)
    if not cone.reactions:
        return []
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        consistent_cone = fastcc(
            cone,
            flux_threshold=1.0,
            zero_cutoff=model.tolerance,
        )
    reportable_ids = {reaction.id for reaction in _internal_reactions(model)}
    return sorted(
        reaction.id for reaction in consistent_cone.reactions if reaction.id in reportable_ids
    )


def _elemental_balance(model):
    """Measure C/N/P/S balance using the established validate.py convention."""
    from validate import CORE_ELEMS, parse_formula

    unbalanced = []
    details = {}
    checked = 0
    skipped_missing_formula = 0
    skipped_objective_formula = 0

    # A single-objective biomass equation often contains a pseudo-metabolite
    # without a formula.  It is counted as a skipped, not unbalanced, reaction.
    try:
        from cobra.util.solver import linear_reaction_coefficients

        objective_ids = {reaction.id for reaction in linear_reaction_coefficients(model)}
    except Exception:
        objective_ids = set()

    for reaction in _internal_reactions(model):
        if not reaction.metabolites or not all(met.formula for met in reaction.metabolites):
            skipped_missing_formula += 1
            if reaction.id in objective_ids:
                skipped_objective_formula += 1
            continue

        checked += 1
        deltas = defaultdict(float)
        for metabolite, coefficient in reaction.metabolites.items():
            for element, atom_count in parse_formula(metabolite.formula).items():
                if element in CORE_ELEMS:
                    deltas[element] += coefficient * atom_count

        imbalance = {
            element: value
            for element, value in deltas.items()
            if abs(value) > BALANCE_TOLERANCE
        }
        if imbalance:
            unbalanced.append(reaction.id)
            details[reaction.id] = "; ".join(
                f"{element}:{value:+g}" for element, value in sorted(imbalance.items())
            )

    skipped_note = (
        "口径：仅检查非 boundary/EX/DM/SK 内部反应，且反应中每个代谢物都有 formula；"
        "按现有 gem_validate G2 的 C/N/P/S 严格配平口径计数（H/O 的质子化约定不计入"
        "unbalanced_count）。目标/生物质方程如化学式完整则照常检查；仅当其包含无 formula 的"
        "伪代谢物时跳过并单列说明。"
        f"跳过 {skipped_missing_formula} 条缺公式反应"
        + (
            f"（其中 {skipped_objective_formula} 条为目标/生物质式且含伪代谢物）"
            if skipped_objective_formula
            else ""
        )
        + "。"
    )
    return checked, sorted(unbalanced), details, skipped_note


def _orphan_and_dead_end_metabolites(model):
    """Return internal-only metabolites that have only one stoichiometric role."""
    orphan, dead_end = [], []
    skipped_boundary_connected = 0

    for metabolite in model.metabolites:
        reactions = list(metabolite.reactions)
        # Boundary reactions represent environment/source/sink.  A metabolite
        # attached to one is intentionally not labelled a network dead end.
        if not reactions or any(_is_boundary(reaction) for reaction in reactions):
            if reactions:
                skipped_boundary_connected += 1
            continue

        produces = any(reaction.metabolites[metabolite] > 0 for reaction in reactions)
        consumes = any(reaction.metabolites[metabolite] < 0 for reaction in reactions)
        if consumes and not produces:
            orphan.append(metabolite.id)
        elif produces and not consumes:
            dead_end.append(metabolite.id)

    note = (
        "孤儿=在内部反应中仅被消耗、无生成反应；死端=仅被生成、无消耗反应。"
        "任何连接 boundary/EX/DM/SK 反应的代谢物均排除，避免把环境供给或排出误判为网络断点；"
        f"本模型因此排除 {skipped_boundary_connected} 个代谢物。"
    )
    return sorted(orphan), sorted(dead_end), note


def _gpr_coverage(model):
    reactions = list(model.reactions)
    total = len(reactions)
    with_gpr = sum(bool((reaction.gene_reaction_rule or "").strip()) for reaction in reactions)
    return with_gpr, total, (with_gpr / total if total else 0.0)


def _annotation_coverage(model):
    reactions = list(model.reactions)
    metabolites = list(model.metabolites)
    annotated_reactions = [reaction.id for reaction in reactions if bool(reaction.annotation)]
    annotated_metabolites = [metabolite.id for metabolite in metabolites if bool(metabolite.annotation)]
    reaction_fraction = len(annotated_reactions) / len(reactions) if reactions else 0.0
    metabolite_fraction = len(annotated_metabolites) / len(metabolites) if metabolites else 0.0
    note = (
        "SBML annotation 统计口径：cobra Reaction.annotation / Metabolite.annotation 非空即计为已注释；"
        "不将 id、name、formula 或 gene_reaction_rule 视为 annotation。"
    )
    return (
        reaction_fraction,
        metabolite_fraction,
        annotated_reactions,
        annotated_metabolites,
        note,
    )


def _connectivity(model):
    """Count connected components in the internal metabolite-reaction bipartite graph."""
    adjacency = defaultdict(set)
    nodes = set()
    for reaction in _internal_reactions(model):
        reaction_node = f"reaction:{reaction.id}"
        nodes.add(reaction_node)
        for metabolite in reaction.metabolites:
            metabolite_node = f"metabolite:{metabolite.id}"
            nodes.add(metabolite_node)
            adjacency[reaction_node].add(metabolite_node)
            adjacency[metabolite_node].add(reaction_node)

    components = 0
    largest = 0
    unseen = set(nodes)
    while unseen:
        components += 1
        start = unseen.pop()
        queue = deque([start])
        component_size = 1
        while queue:
            node = queue.popleft()
            for neighbor in adjacency[node]:
                if neighbor in unseen:
                    unseen.remove(neighbor)
                    queue.append(neighbor)
                    component_size += 1
        largest = max(largest, component_size)

    fraction = largest / len(nodes) if nodes else 0.0
    note = (
        "connectivity 以非 boundary/EX/DM/SK 反应和其代谢物构成二部图；"
        "components 与 largest_component_fraction 按该图的全部节点数（反应节点+代谢物节点）计算。"
    )
    return components, fraction, note


def _write_export_csv(path, records):
    """Write long-form complete lists; JSON samples remain deliberately capped."""
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=("check", "item_id", "detail"))
        writer.writeheader()
        writer.writerows(records)


def quality_report(model_path, medium=None, checks=None, export_csv=None):
    """Build a gem-qi-v1 report for one SBML model.

    When ``checks`` is a subset, only requested metric objects are emitted and
    only their assessable scoring components enter the quality-index denominator.
    This prevents an omitted expensive check from being mistaken for missing data.
    """
    if not model_path or not os.path.isfile(model_path):
        raise ValueError(f"model file not found: {model_path}")
    if export_csv is not None and not isinstance(export_csv, str):
        raise ValueError("export_csv must be a path string when provided")

    selected = _selected_checks(checks)
    model = silent_read_sbml(model_path)
    medium_summary, unresolved_medium = _configure_medium(model, medium)

    metrics = {}
    failed_checks = []
    not_assessable = []
    scores = {}
    export_records = []
    notes = [QUALITY_INDEX_NOTE]

    if medium is None:
        notes.append("介质口径：未提供 medium，blocked_reactions 按 SBML 模型自带的当前 exchange bounds 计算。")
    else:
        notes.append(
            "介质口径：已关闭全部 boundary/EX/DM/SK 摄取下界，再应用请求介质；"
            f"preset={medium_summary['preset']!r}，resolved_exchanges={medium_summary['resolved_exchanges']}。"
        )
    if unresolved_medium:
        notes.append("未解析的介质成分未施加：" + ", ".join(sorted(unresolved_medium)) + "。")

    if "blocked_reactions" in selected:
        blocked_ids, total_checked = _blocked_reaction_ids(model)
        fraction = len(blocked_ids) / total_checked if total_checked else 0.0
        metrics["blocked_reactions"] = {
            "count": len(blocked_ids),
            "total_checked": total_checked,
            "fraction": fraction,
            "ids_sample": blocked_ids[:ID_SAMPLE_LIMIT],
        }
        export_records.extend(
            {"check": "blocked_reactions", "item_id": reaction_id, "detail": "find_blocked_reactions"}
            for reaction_id in blocked_ids
        )
        if total_checked:
            scores["blocked"] = max(0.0, 1.0 - fraction / 0.5)
            if fraction > 0.2:
                failed_checks.append("blocked_reactions")
        else:
            not_assessable.append("blocked_reactions")
        notes.append(
            "blocked_reactions 使用 cobra.find_blocked_reactions；仅计非 boundary/EX/DM/SK 反应，"
            "并显式 processes=1 以兼容 Windows FVA。"
        )

    if "cyclic_reactions" in selected:
        cyclic_ids = _cyclic_reaction_ids(model)
        metrics["cyclic_reactions"] = {
            "count": len(cyclic_ids),
            "ids_sample": cyclic_ids[:ID_SAMPLE_LIMIT],
        }
        export_records.extend(
            {"check": "cyclic_reactions", "item_id": reaction_id, "detail": "cobra.fastcc_direction_cone"}
            for reaction_id in cyclic_ids
        )
        if _internal_reactions(model):
            scores["cyclic"] = max(0.0, 1.0 - len(cyclic_ids) / 50.0)
            if cyclic_ids:
                failed_checks.append("cyclic_reactions")
        else:
            not_assessable.append("cyclic_reactions")
        notes.append(
            "cyclic_reactions 在独立的化学计量+反应方向锥上以 cobra.fastcc 计算："
            "非 boundary 反应按可用方向正规化，反向专用反应翻转化学计量，fixed-zero 反应排除。"
            "该口径等价于 cobra.find_cyclic_reactions 所声明的潜在稳态环并集，且刻意忽略介质、"
            "数值 bounds 大小与源模型额外约束；因此它不是当前培养基下每个环都必然可行的通量证明。"
        )

    if "elemental_balance" in selected:
        checked, unbalanced_ids, imbalance_details, skipped_note = _elemental_balance(model)
        metrics["elemental_balance"] = {
            "checked": checked,
            "balanced": checked - len(unbalanced_ids),
            "unbalanced_count": len(unbalanced_ids),
            "unbalanced_ids_sample": unbalanced_ids[:ID_SAMPLE_LIMIT],
            "skipped_note": skipped_note,
        }
        export_records.extend(
            {
                "check": "elemental_balance",
                "item_id": reaction_id,
                "detail": imbalance_details[reaction_id],
            }
            for reaction_id in unbalanced_ids
        )
        if checked:
            scores["balance"] = (checked - len(unbalanced_ids)) / checked
            if unbalanced_ids:
                failed_checks.append("elemental_balance")
        else:
            not_assessable.append("elemental_balance")

    if "orphan_metabolites" in selected or "dead_end_metabolites" in selected:
        orphan_ids, dead_end_ids, topology_note = _orphan_and_dead_end_metabolites(model)
        if "orphan_metabolites" in selected:
            metrics["orphan_metabolites"] = {
                "count": len(orphan_ids),
                "ids_sample": orphan_ids[:ID_SAMPLE_LIMIT],
            }
            export_records.extend(
                {"check": "orphan_metabolites", "item_id": metabolite_id, "detail": "only_consumed"}
                for metabolite_id in orphan_ids
            )
        if "dead_end_metabolites" in selected:
            metrics["dead_end_metabolites"] = {
                "count": len(dead_end_ids),
                "ids_sample": dead_end_ids[:ID_SAMPLE_LIMIT],
            }
            export_records.extend(
                {"check": "dead_end_metabolites", "item_id": metabolite_id, "detail": "only_produced"}
                for metabolite_id in dead_end_ids
            )
        notes.append(topology_note)

    if "gpr_coverage" in selected:
        with_gpr, total, fraction = _gpr_coverage(model)
        metrics["gpr_coverage"] = {
            "reactions_with_gpr": with_gpr,
            "reactions_total": total,
            "fraction": fraction,
        }
        missing_gpr = sorted(
            reaction.id for reaction in model.reactions if not (reaction.gene_reaction_rule or "").strip()
        )
        export_records.extend(
            {"check": "gpr_coverage", "item_id": reaction_id, "detail": "missing_gene_reaction_rule"}
            for reaction_id in missing_gpr
        )
        if total:
            scores["gpr"] = fraction
            if fraction < 0.5:
                failed_checks.append("gpr_coverage")
        else:
            not_assessable.append("gpr_coverage")
        notes.append("gpr_coverage 按全部 SBML reactions 计；非空 gene_reaction_rule 视为已有 GPR。")

    if "annotation_coverage" in selected:
        (
            reaction_fraction,
            metabolite_fraction,
            annotated_reactions,
            annotated_metabolites,
            annotation_note,
        ) = _annotation_coverage(model)
        metrics["annotation_coverage"] = {
            "reaction_annotation_fraction": reaction_fraction,
            "metabolite_annotation_fraction": metabolite_fraction,
            "note": annotation_note,
        }
        export_records.extend(
            {"check": "annotation_coverage", "item_id": reaction_id, "detail": "reaction_annotation_present"}
            for reaction_id in annotated_reactions
        )
        export_records.extend(
            {"check": "annotation_coverage", "item_id": metabolite_id, "detail": "metabolite_annotation_present"}
            for metabolite_id in annotated_metabolites
        )
        if not annotated_reactions and not annotated_metabolites:
            not_assessable.append("annotation_coverage")
        elif model.reactions and model.metabolites:
            scores["reaction_annotation"] = reaction_fraction
            scores["metabolite_annotation"] = metabolite_fraction
            if reaction_fraction < 0.5 or metabolite_fraction < 0.5:
                failed_checks.append("annotation_coverage")
        else:
            not_assessable.append("annotation_coverage")
        notes.append(annotation_note)

    if "connectivity" in selected:
        components, largest_fraction, connectivity_note = _connectivity(model)
        metrics["connectivity"] = {
            "components": components,
            "largest_component_fraction": largest_fraction,
        }
        if components == 0:
            not_assessable.append("connectivity")
        notes.append(connectivity_note)

    numerator = sum(WEIGHTS[name] * score for name, score in scores.items())
    denominator = sum(WEIGHTS[name] for name in scores)
    quality_index = 100.0 * numerator / denominator if denominator else 0.0
    if not denominator:
        notes.append("没有可评估的计分项，因此 quality_index 返回 0.0 且不可作比较。")

    if export_csv:
        _write_export_csv(export_csv, export_records)
        notes.append(f"完整清单已写入 export_csv：{export_csv}。")

    return {
        "model": model_path,
        "model_hash": _model_hash(model_path),
        "medium": medium_summary,
        "metrics": metrics,
        "quality_index": quality_index,
        "score_profile_version": "gem-qi-v1",
        "weights": dict(WEIGHTS),
        "failed_checks": failed_checks,
        "not_assessable_checks": not_assessable,
        "notes": notes,
    }
