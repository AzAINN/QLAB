"""Pure, boundary-safe calibration scoring for realized risk views.

The caller owns horizon selection and supplies both the post-view returns panel
and the pre-view risk baseline.  This module deliberately scores only
volatility, correlation, and two-sided tail mass; it has no return-direction
or price-target scoring path.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


_VIEW_KINDS = ("vol", "corr", "tail")
_BASELINE_FIELDS = {
    "vol": ("pre_view_vol", "pre_vol", "baseline_vol"),
    "corr": ("pre_view_corr", "pre_corr", "baseline_corr"),
    "tail": (
        "pre_view_tail_mass",
        "pre_tail_mass",
        "baseline_tail_mass",
    ),
}
_FORBIDDEN_FIELD_TOKENS = ("return", "price", "alpha")
_TAIL_Z = 2.0


def _kind_from_label(label: str) -> str:
    if not isinstance(label, str) or not label:
        raise ValueError("view_label must be a non-empty string")
    for kind in _VIEW_KINDS:
        if (
            label == kind
            or label.startswith(f"{kind}(")
            or label.startswith(f"{kind}:")
        ):
            return kind
    raise ValueError(
        "view_label must identify a vol, corr, or tail risk view"
    )


@dataclass(frozen=True)
class ViewScore:
    """One realized risk-view score.

    ``magnitude_score`` is signed: positive values are movement in the claimed
    direction, negative values are movement against it, and zero is no
    directional realization.
    """

    view_label: str
    direction_correct: bool
    realized_value: float
    expected_direction: str
    magnitude_score: float

    def __post_init__(self) -> None:
        kind = _kind_from_label(self.view_label)
        if not isinstance(self.direction_correct, (bool, np.bool_)):
            raise TypeError("direction_correct must be a boolean")
        if isinstance(self.realized_value, (bool, np.bool_)):
            raise TypeError("realized_value must be numeric")
        if isinstance(self.magnitude_score, (bool, np.bool_)):
            raise TypeError("magnitude_score must be numeric")
        try:
            realized = float(self.realized_value)
            magnitude = float(self.magnitude_score)
        except (TypeError, ValueError) as exc:
            raise TypeError(
                "realized_value and magnitude_score must be numeric"
            ) from exc
        if not np.isfinite(realized):
            raise ValueError("realized_value must be finite")
        if not np.isfinite(magnitude) or not -1.0 <= magnitude <= 1.0:
            raise ValueError("magnitude_score must be finite and in [-1, 1]")

        expected = (
            {"up", "down"} if kind in {"vol", "corr"}
            else {"fatter", "thinner"}
        )
        if self.expected_direction not in expected:
            raise ValueError(
                f"{kind} expected_direction must be one of {sorted(expected)}"
            )
        if kind in {"vol", "tail"} and realized < 0.0:
            raise ValueError(f"{kind} realized_value cannot be negative")
        if kind == "corr" and not -1.0 <= realized <= 1.0:
            raise ValueError("corr realized_value must be in [-1, 1]")
        if kind == "tail" and realized > 1.0:
            raise ValueError("tail realized_value must be in [0, 1]")

        correct = bool(self.direction_correct)
        if correct != (magnitude > 0.0):
            raise ValueError(
                "direction_correct must agree with the sign of magnitude_score"
            )
        object.__setattr__(self, "direction_correct", correct)
        object.__setattr__(self, "realized_value", realized)
        object.__setattr__(self, "magnitude_score", magnitude)

    @property
    def view_kind(self) -> str:
        """Return the risk kind encoded by ``view_label``."""

        return _kind_from_label(self.view_label)


def _finite_number(payload: dict, field: str) -> float:
    if field not in payload:
        raise ValueError(f"view_payload is missing required field {field!r}")
    value = payload[field]
    if isinstance(value, (bool, np.bool_)):
        raise TypeError(f"{field} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{field} must be numeric") from exc
    if not np.isfinite(number):
        raise ValueError(f"{field} must be finite")
    return number


def _baseline(payload: dict, kind: str) -> float:
    fields = _BASELINE_FIELDS[kind]
    present = [field for field in fields if field in payload]
    if len(present) != 1:
        raise ValueError(
            f"{kind} view_payload must contain exactly one pre-view baseline "
            f"field from {list(fields)}"
        )
    return _finite_number(payload, present[0])


def _ticker(payload: dict, field: str, index: dict[str, int]) -> tuple[str, int]:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty ticker string")
    if value not in index:
        raise ValueError(f"ticker {value!r} is absent from realized_panel")
    return value, index[value]


def _validated_inputs(
    view_kind: str,
    view_payload: dict,
    realized_panel,
    tickers,
) -> tuple[np.ndarray, dict[str, int]]:
    if not isinstance(view_kind, str) or view_kind not in _VIEW_KINDS:
        raise ValueError(
            f"view_kind must be one of {list(_VIEW_KINDS)}; "
            "return and price views are not scoreable"
        )
    if not isinstance(view_payload, dict):
        raise TypeError("view_payload must be a dict")
    if not all(isinstance(field, str) for field in view_payload):
        raise TypeError("view_payload field names must be strings")
    forbidden = sorted(
        field
        for field in view_payload
        if any(token in field.lower() for token in _FORBIDDEN_FIELD_TOKENS)
    )
    if forbidden:
        raise ValueError(
            f"view_payload contains forbidden return/price fields {forbidden}"
        )
    payload_kind = view_payload.get("type")
    if payload_kind is not None and payload_kind != view_kind:
        raise ValueError(
            f"view_payload type {payload_kind!r} does not match "
            f"view_kind {view_kind!r}"
        )

    try:
        panel = np.asarray(realized_panel, dtype=float)
    except (TypeError, ValueError) as exc:
        raise TypeError("realized_panel must contain numeric returns") from exc
    if panel.ndim != 2:
        raise ValueError("realized_panel must be a T x N returns matrix")
    if panel.shape[0] < 2 or panel.shape[1] < 1:
        raise ValueError(
            "realized_panel must contain at least two rows and one column"
        )
    if not np.all(np.isfinite(panel)):
        raise ValueError("realized_panel contains non-finite returns")

    if not isinstance(tickers, (list, tuple)):
        raise TypeError("tickers must be a list or tuple of strings")
    if (
        len(tickers) != panel.shape[1]
        or not all(isinstance(ticker, str) and ticker for ticker in tickers)
    ):
        raise ValueError("tickers must match realized_panel columns")
    if len(set(tickers)) != len(tickers):
        raise ValueError("tickers must be unique")
    return panel, {ticker: column for column, ticker in enumerate(tickers)}


def _target_score(
    baseline: float, target: float, realized: float
) -> tuple[str, bool, float]:
    requested_move = target - baseline
    tolerance = 8.0 * np.finfo(float).eps * max(
        1.0, abs(baseline), abs(target), abs(realized)
    )
    if abs(requested_move) <= tolerance:
        raise ValueError("view target must differ from its pre-view baseline")

    expected_sign = 1.0 if requested_move > 0.0 else -1.0
    expected_direction = "up" if expected_sign > 0.0 else "down"
    progress = expected_sign * (realized - baseline)
    if abs(progress) <= tolerance:
        progress = 0.0
    magnitude = float(np.clip(progress / abs(requested_move), -1.0, 1.0))
    return expected_direction, magnitude > 0.0, magnitude


def _tail_mass(values: np.ndarray) -> float:
    centered = values - float(np.mean(values))
    sigma = float(np.std(values, ddof=0))
    if sigma == 0.0:
        return 0.0
    return float(np.mean(np.abs(centered) > _TAIL_Z * sigma))


def _tail_score(
    baseline: float, realized: float, direction: str
) -> tuple[bool, float]:
    expected_sign = 1.0 if direction == "fatter" else -1.0
    direction_room = 1.0 - baseline if expected_sign > 0.0 else baseline
    if direction_room <= 0.0:
        raise ValueError(
            f"a {direction} tail view cannot move from baseline {baseline}"
        )

    progress = expected_sign * (realized - baseline)
    tolerance = 8.0 * np.finfo(float).eps
    if abs(progress) <= tolerance:
        progress = 0.0
    if progress >= 0.0:
        scale = direction_room
    else:
        scale = baseline if expected_sign > 0.0 else 1.0 - baseline
    magnitude = 0.0 if progress == 0.0 else float(
        np.clip(progress / scale, -1.0, 1.0)
    )
    return magnitude > 0.0, magnitude


def view_realization(
    view_kind: str,
    view_payload: dict,
    realized_panel,
    tickers,
) -> ViewScore:
    """Score one risk view against returns strictly after the view's ``as_of``.

    Required payload fields are:

    - vol: ``ticker``, ``target_vol``, and ``pre_view_vol``;
    - corr: ``ticker_a``, ``ticker_b``, ``target_corr``, and
      ``pre_view_corr``;
    - tail: ``ticker``, ``direction``, and ``pre_view_tail_mass``.

    The shorter ``pre_*`` and ``baseline_*`` spellings are accepted for
    persistence-layer compatibility, but supplying more than one baseline is
    rejected as ambiguous.
    """

    panel, index = _validated_inputs(
        view_kind, view_payload, realized_panel, tickers
    )
    baseline = _baseline(view_payload, view_kind)

    if view_kind == "vol":
        ticker, column = _ticker(view_payload, "ticker", index)
        target = _finite_number(view_payload, "target_vol")
        if baseline <= 0.0 or target <= 0.0:
            raise ValueError(
                "pre-view and target volatility must be positive"
            )
        realized = float(np.std(panel[:, column], ddof=0))
        direction, correct, magnitude = _target_score(
            baseline, target, realized
        )
        label = f"vol({ticker}→{target:.4f})"
    elif view_kind == "corr":
        ticker_a, column_a = _ticker(view_payload, "ticker_a", index)
        ticker_b, column_b = _ticker(view_payload, "ticker_b", index)
        if ticker_a == ticker_b:
            raise ValueError("correlation view needs two distinct tickers")
        target = _finite_number(view_payload, "target_corr")
        if not -1.0 <= baseline <= 1.0:
            raise ValueError("pre-view correlation must be in [-1, 1]")
        if not -0.95 <= target <= 0.95:
            raise ValueError("target_corr must be in [-0.95, 0.95]")

        left = panel[:, column_a] - float(np.mean(panel[:, column_a]))
        right = panel[:, column_b] - float(np.mean(panel[:, column_b]))
        denominator = float(
            np.sqrt(np.dot(left, left) * np.dot(right, right))
        )
        if denominator == 0.0:
            raise ValueError(
                "realized correlation is undefined for a constant series"
            )
        realized = float(
            np.clip(np.dot(left, right) / denominator, -1.0, 1.0)
        )
        direction, correct, magnitude = _target_score(
            baseline, target, realized
        )
        label = (
            f"corr({ticker_a},{ticker_b}→{target:+.2f})"
        )
    else:
        ticker, column = _ticker(view_payload, "ticker", index)
        direction = view_payload.get("direction")
        if direction not in {"fatter", "thinner"}:
            raise ValueError("direction must be 'fatter' or 'thinner'")
        if not 0.0 <= baseline <= 1.0:
            raise ValueError("pre-view tail mass must be in [0, 1]")
        realized = _tail_mass(panel[:, column])
        correct, magnitude = _tail_score(
            baseline, realized, direction
        )
        label = f"tail({ticker} {direction})"

    return ViewScore(
        view_label=label,
        direction_correct=correct,
        realized_value=realized,
        expected_direction=direction,
        magnitude_score=magnitude,
    )


def _validated_scores(scores: list[ViewScore]) -> list[ViewScore]:
    if not isinstance(scores, list):
        raise TypeError("scores must be a list of ViewScore objects")
    for index, score in enumerate(scores):
        if not isinstance(score, ViewScore):
            raise TypeError(f"scores[{index}] must be a ViewScore")
    return scores


def _aggregate(scores: list[ViewScore]) -> dict[str, float | int | None]:
    if not scores:
        return {"hit_rate": None, "mean_magnitude": None, "n": 0}
    count = len(scores)
    return {
        "hit_rate": float(
            sum(score.direction_correct for score in scores) / count
        ),
        "mean_magnitude": float(
            sum(score.magnitude_score for score in scores) / count
        ),
        "n": count,
    }


def calibration_summary(scores: list[ViewScore]) -> dict:
    """Aggregate realized calibration overall and by risk-view kind."""

    validated = _validated_scores(scores)
    summary = _aggregate(validated)
    summary["by_view_kind"] = {
        kind: _aggregate(
            [score for score in validated if score.view_kind == kind]
        )
        for kind in _VIEW_KINDS
        if any(score.view_kind == kind for score in validated)
    }
    return summary


def reliability_score(scores: list[ViewScore]) -> float:
    """Return the empirical hit rate for a caller-selected source cohort."""

    validated = _validated_scores(scores)
    if not validated:
        raise ValueError("reliability_score requires at least one ViewScore")
    return float(
        sum(score.direction_correct for score in validated) / len(validated)
    )
