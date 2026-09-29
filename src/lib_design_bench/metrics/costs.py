"""Normalize trial costs from retained token evidence and explicit, validated model rates."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

from lib_design_bench.models.reports import UsageReport


@dataclass(frozen=True)
class CostRates:
    """USD prices per million uncached input, output, and cached input tokens."""

    input_per_million: float
    output_per_million: float
    cache_input_per_million: float

    def __post_init__(self) -> None:
        """Reject rates that cannot represent nonnegative dollar prices."""
        values = (
            self.input_per_million,
            self.output_per_million,
            self.cache_input_per_million,
        )
        if any(not math.isfinite(value) or value < 0 for value in values):
            raise ValueError("cost rates must be finite and nonnegative")


@dataclass(frozen=True)
class ImplementorPricing:
    """One rate set for the Evaluation Phase cells of one implementor, or of all.

    A `None` implementor prices every Evaluation Phase cell of the target, which only
    a standalone evaluation run can mean unambiguously.
    """

    rates: CostRates
    implementor: str | None


@dataclass(frozen=True)
class ModelPricing:
    """Rate sets applied to every cell by the model name that produced it.

    Pricing belongs to a model rather than to the arm that mounted it, so one
    declaration reprices every implementor and the authoring model together,
    and is reusable across runs. A model the target never ran is priced by
    nobody and changes nothing.
    """

    rates: Mapping[str, CostRates]

    def __post_init__(self) -> None:
        """Reject a policy that prices nothing."""
        if not self.rates:
            raise ValueError("model pricing must price at least one model")

    def for_model(self, model_name: str | None) -> CostRates | None:
        """Return the rates this policy prices one cell's model at, if any."""
        return None if model_name is None else self.rates.get(model_name)


Pricing = ImplementorPricing | ModelPricing


def persisted_cost_rates(usage: UsageReport | None) -> CostRates | None:
    """Recover the full pricing policy retained on one LDB trial result."""
    if (
        usage is None
        or usage.input_cost_per_million is None
        or usage.output_cost_per_million is None
        or usage.cache_input_cost_per_million is None
    ):
        return None
    return CostRates(
        input_per_million=usage.input_cost_per_million,
        output_per_million=usage.output_cost_per_million,
        cache_input_per_million=usage.cache_input_cost_per_million,
    )


def standardize_usage_cost(usage: UsageReport, rates: CostRates) -> UsageReport:
    """Preserve reported cost and price complete token evidence at fixed rates."""
    was_priced = persisted_cost_rates(usage) is not None
    reported = usage.reported_cost_usd if was_priced else usage.cost_usd
    input_tokens = (
        usage.uncached_input_tokens
        if usage.uncached_input_tokens is not None
        else usage.input_tokens
    )
    output_tokens = usage.output_tokens
    cache_input_tokens = usage.cache_input_tokens
    standardized = (
        None
        if input_tokens is None or output_tokens is None or cache_input_tokens is None
        else (
            input_tokens * rates.input_per_million
            + output_tokens * rates.output_per_million
            + cache_input_tokens * rates.cache_input_per_million
        )
        / 1_000_000
    )
    return UsageReport.model_validate(
        {
            **usage.model_dump(),
            "cost_usd": reported if standardized is None else standardized,
            "reported_cost_usd": reported,
            "standardized_cost_usd": standardized,
            "input_cost_per_million": rates.input_per_million,
            "output_cost_per_million": rates.output_per_million,
            "cache_input_cost_per_million": rates.cache_input_per_million,
        }
    )


_RATE_FIELDS = ("input", "output", "cache_input")


def load_model_pricing(path: Path) -> ModelPricing:
    """Load complete, finite USD-per-million rates keyed by model name."""
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ValueError(str(error)) from error
    if not isinstance(document, dict):
        raise ValueError(f"Pricing config must be a mapping: {path}")
    models = document.get("models")
    if not isinstance(models, dict) or not models:
        raise ValueError(f"Pricing config `models` must be a non-empty mapping: {path}")
    unknown = sorted(set(document) - {"models"})
    if unknown:
        raise ValueError(
            f"Unknown pricing config keys: {', '.join(unknown)}; only `models` is read"
        )
    rates: dict[str, CostRates] = {}
    for name, entry in models.items():
        if not isinstance(entry, dict) or set(entry) != set(_RATE_FIELDS):
            raise ValueError(
                f"Model {name!r} must declare exactly "
                f"{', '.join(_RATE_FIELDS)} USD-per-million rates"
            )
        values = tuple(entry[field] for field in _RATE_FIELDS)
        if any(
            isinstance(value, bool) or not isinstance(value, int | float)
            for value in values
        ):
            raise ValueError(f"Model {name!r} rates must be numbers")
        rates[str(name)] = CostRates(
            input_per_million=float(values[0]),
            output_per_million=float(values[1]),
            cache_input_per_million=float(values[2]),
        )
    return ModelPricing(rates=rates)
