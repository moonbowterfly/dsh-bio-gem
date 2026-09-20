"""Flux-space sampling primitives for the ``sample`` gem operation."""
import contextlib
import hashlib
import io
import os
import time

import numpy as np
from cobra.sampling import ACHRSampler
from cobra.util.solver import linear_reaction_coefficients

from gapfind import expand_medium, resolve_medium
from silentio import silent_read_sbml


EX_PREFIXES = ("EX_", "DM_", "SK_")
FLUX_TOLERANCE = 1e-9
MIN_SAMPLES = 10
MAX_SAMPLES = 20000


def _model_hash(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()[:16]


def _is_boundary(reaction):
    return reaction.boundary or reaction.id.startswith(EX_PREFIXES)


def _configure_medium(model, medium):
    """Apply only an explicitly supplied medium; preserve SBML defaults otherwise."""
    if medium is not None and not isinstance(medium, dict):
        raise ValueError("medium must be an object when provided")

    expanded, preset = expand_medium(medium)
    resolved, unresolved = resolve_medium(model, expanded) if expanded else ({}, [])
    if medium is not None:
        for reaction in model.reactions:
            if _is_boundary(reaction):
                reaction.lower_bound = 0.0
        for reaction_id, lower_bound in resolved.items():
            model.reactions.get_by_id(reaction_id).lower_bound = lower_bound
    return {"preset": preset, "resolved_exchanges": len(resolved)}, unresolved


def _require_int(name, value, minimum=None, maximum=None):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be <= {maximum}")
    return value


def _growth_reaction(model):
    """Select the strongest positive linear objective reaction deterministically."""
    coefficients = linear_reaction_coefficients(model)
    if not coefficients:
        raise ValueError("model needs a linear objective to identify the growth reaction")

    candidates = [(reaction, float(coefficient)) for reaction, coefficient in coefficients.items()]
    positive = [(reaction, coefficient) for reaction, coefficient in candidates if coefficient > 0]
    pool = positive or candidates
    reaction, _coefficient = sorted(pool, key=lambda item: (-abs(item[1]), item[0].id))[0]
    return reaction


def _describe(values):
    values = np.asarray(values, dtype=float)
    return {
        "median": float(np.quantile(values, 0.5)),
        "mean": float(np.mean(values)),
        "min": float(np.min(values)),
        "max": float(np.max(values)),
        "q05": float(np.quantile(values, 0.05)),
        "q25": float(np.quantile(values, 0.25)),
        "q75": float(np.quantile(values, 0.75)),
        "q95": float(np.quantile(values, 0.95)),
    }


def _reaction_stats(values):
    values = np.asarray(values, dtype=float)
    return {
        "median": float(np.quantile(values, 0.5)),
        "q05": float(np.quantile(values, 0.05)),
        "q95": float(np.quantile(values, 0.95)),
        "sign_probability": float(np.mean(values > FLUX_TOLERANCE)),
        "near_zero_fraction": float(np.mean(np.abs(values) <= FLUX_TOLERANCE)),
    }


def _selected_reactions(samples, growth_reaction_id, reactions):
    if reactions is not None:
        if not isinstance(reactions, list) or any(not isinstance(item, str) for item in reactions):
            raise ValueError("reactions must be an array of reaction IDs")
        selected = []
        for reaction_id in reactions:
            if reaction_id not in samples.columns:
                raise ValueError(f"reaction not found in model: {reaction_id}")
            if reaction_id not in selected:
                selected.append(reaction_id)
        return selected

    iqr = samples.quantile(0.75) - samples.quantile(0.25)
    ranked = sorted(
        (reaction_id for reaction_id in samples.columns if reaction_id != growth_reaction_id),
        key=lambda reaction_id: (-float(iqr[reaction_id]), reaction_id),
    )
    return [growth_reaction_id] + ranked[:20]


def _validate_requested_reactions(model, reactions):
    """Reject malformed target lists before expensive ACHR warmup starts."""
    if reactions is None:
        return
    if not isinstance(reactions, list) or any(not isinstance(item, str) for item in reactions):
        raise ValueError("reactions must be an array of reaction IDs")
    for reaction_id in reactions:
        if reaction_id not in model.reactions:
            raise ValueError(f"reaction not found in model: {reaction_id}")


def _method(method):
    if method is None:
        method = "auto"
    if not isinstance(method, str):
        raise ValueError("method must be one of auto, achr, optgp")
    normalized = method.lower()
    if normalized not in {"auto", "achr", "optgp"}:
        raise ValueError("method must be one of auto, achr, optgp")
    if normalized == "auto":
        return "achr"
    if normalized == "optgp" and os.name == "nt":
        # OptGP creates a worker pool.  gem_ops is deliberately import-safe,
        # but this operation must never expose Windows callers to an accidental
        # spawn loop until that execution route has its own verified harness.
        raise ValueError("method 'optgp' is not supported on Windows; use 'achr' or 'auto'")
    return normalized


def sample_fluxes(
    model_path,
    medium=None,
    n=1000,
    method="auto",
    thinning=100,
    growth_floor_fraction=None,
    reactions=None,
    seed=42,
    export_csv=None,
):
    """Sample a model's feasible flux space with a deterministic ACHR default."""
    if not model_path or not os.path.isfile(model_path):
        raise ValueError(f"model file not found: {model_path}")
    n = _require_int("n", n, MIN_SAMPLES, MAX_SAMPLES)
    thinning = _require_int("thinning", thinning, 1)
    seed = _require_int("seed", seed)
    if export_csv is not None and not isinstance(export_csv, str):
        raise ValueError("export_csv must be a path string when provided")
    if growth_floor_fraction is not None:
        if isinstance(growth_floor_fraction, bool) or not isinstance(growth_floor_fraction, (int, float)):
            raise ValueError("growth_floor_fraction must be a number strictly between 0 and 1")
        growth_floor_fraction = float(growth_floor_fraction)
        if not 0.0 < growth_floor_fraction < 1.0:
            raise ValueError("growth_floor_fraction must be strictly between 0 and 1")

    method_used = _method(method)
    started = time.perf_counter()
    base_model = silent_read_sbml(model_path)
    configured_model = base_model.copy()
    medium_summary, unresolved_medium = _configure_medium(configured_model, medium)

    growth_reaction = _growth_reaction(configured_model)
    fba_solution = configured_model.optimize()
    if fba_solution.status != "optimal":
        raise ValueError(f"FBA under the selected medium is not optimal: {fba_solution.status}")
    max_growth = float(fba_solution.fluxes[growth_reaction.id])

    # Every sampling run gets an independent model copy.  The growth-floor
    # branch therefore cannot leak modified bounds into the caller or a retry.
    sampling_model = configured_model.copy()
    if growth_floor_fraction is not None:
        if max_growth <= FLUX_TOLERANCE:
            raise ValueError("cannot apply growth_floor_fraction because maximum growth is not positive")
        sampling_growth = sampling_model.reactions.get_by_id(growth_reaction.id)
        sampling_growth.lower_bound = growth_floor_fraction * max_growth

    _validate_requested_reactions(sampling_model, reactions)

    # The only fully supported execution route for the Windows target is ACHR.
    # A non-Windows future caller can still ask for OptGP explicitly.
    # Sampling libraries can emit backend diagnostics.  Keep them off stdout so
    # gem_ops retains its one-JSON-object protocol.
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        if method_used == "achr":
            sampler = ACHRSampler(sampling_model, thinning=thinning, seed=seed)
        else:
            from cobra.sampling import OptGPSampler

            sampler = OptGPSampler(sampling_model, thinning=thinning, processes=1, seed=seed)
        samples = sampler.sample(n, fluxes=True)
    runtime_s = time.perf_counter() - started
    if len(samples) != n:
        raise RuntimeError(f"sampler returned {len(samples)} rows for n={n}")

    validation_codes = sampler.validate(samples.to_numpy())
    n_valid = int(np.sum(validation_codes == "v"))
    selected = _selected_reactions(samples, growth_reaction.id, reactions)
    reaction_summary = {
        reaction_id: _reaction_stats(samples[reaction_id].to_numpy())
        for reaction_id in selected
    }
    growth_summary = _describe(samples[growth_reaction.id].to_numpy())

    if export_csv:
        samples.to_csv(export_csv, index=False)

    if growth_floor_fraction is None:
        boundary_space = "full_feasible_space"
        floor_note = "未施加 growth floor。"
    else:
        boundary_space = "growth_floor_constrained_space"
        floor_note = (
            f"已在独立 model.copy() 上将 {growth_reaction.id} 的下界设为 "
            f"{growth_floor_fraction:.6g} × 当前 FBA 最大生长。"
        )

    boundary_note = (
        "全空间均匀采样 ≠ 生物学上有意义的活跃状态；关心近最优生长态请传 "
        "growth_floor_fraction（如 0.9）。"
        f"当前介质下 {growth_reaction.id} 的 FBA 最大生长为 {max_growth:.9g}。{floor_note}"
    )
    feasibility_note = (
        "n_valid 为 cobra sampler.validate 返回 'v'（同时满足稳态、上下界）的样本数；"
        "validation_failures 为其余 l/u/e 代码的样本数。"
    )
    if unresolved_medium:
        feasibility_note += " 未解析的介质成分未施加：" + ", ".join(sorted(unresolved_medium)) + "。"

    return {
        "model": model_path,
        "model_hash": _model_hash(model_path),
        "method_used": method_used,
        "n_requested": n,
        "n_samples": int(len(samples)),
        "thinning": thinning,
        "seed": seed,
        "runtime_s": runtime_s,
        "growth_reaction": growth_reaction.id,
        "growth": growth_summary,
        "reactions": reaction_summary,
        "feasibility": {
            "n_valid": n_valid,
            "validation_failures": int(len(samples) - n_valid),
            "note": feasibility_note,
        },
        "boundary": {
            "space": boundary_space,
            "growth_floor_fraction": growth_floor_fraction,
            "note": boundary_note,
        },
        "medium": medium_summary,
    }
