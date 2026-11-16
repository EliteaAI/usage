#!/usr/bin/python3
# coding=utf-8

#   Copyright 2026 EPAM Systems
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

""" Complexity Units: pure scoring, labelling and calibration helpers (#7552/#7553) """

import json
import pathlib

FORMULA_VERSION = 1
CONTEXT_CAP_CU = 30.0

DEFAULT_THRESHOLDS = {"medium": 10.0, "high": 30.0, "extreme": 100.0}
THRESHOLD_KEYS = ("medium", "high", "extreme")
# The #7552 defaults sit near p75/p90/p99 of real runs; calibration keeps that meaning
CALIBRATION_QUANTILES = (0.75, 0.90, 0.99)

# Daily histogram: bucket 0 is CU < 1, bucket k is [2^(k-1), 2^k), the last one open-ended
HISTOGRAM_BUCKETS = 13

WEIGHTS_PATH = pathlib.Path(__file__).resolve().parent.parent / "data" / "tool_weights_v1.json"

_WEIGHTS = None


def load_tool_weights(path=WEIGHTS_PATH):
    """The shipped weight map, read once per process."""
    global _WEIGHTS  # pylint: disable=W0603
    if _WEIGHTS is None or path != WEIGHTS_PATH:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        weights = {
            "version": int(data["version"]),
            "default": float(data.get("default", 1)),
            "weights": {str(k): float(v) for k, v in (data.get("weights") or {}).items()},
        }
        if path != WEIGHTS_PATH:
            return weights
        _WEIGHTS = weights
    return _WEIGHTS


def bare_tool_name(name):
    """Tool name without its toolkit-instance prefix (`gitlab-397__read_file` -> `read_file`)."""
    name = str(name or "")
    return name.rsplit("__", 1)[-1] if "__" in name else name


def tool_weight(weights, tool_name, toolkit_type=None):
    """`<toolkit_type>:<tool>` first, then the bare tool name, then the default."""
    table = weights["weights"]
    bare = bare_tool_name(tool_name)
    if toolkit_type:
        key = f"{toolkit_type}:{bare}"
        if key in table:
            return table[key]
    return table.get(bare, weights["default"])


def score_run(peak_input_tokens, llm_calls, tool_counts, weights):
    """CU v1 = min(peak/1000, 30) + llm_calls + sum(weight(tool) per call)."""
    cu_context = min(max(int(peak_input_tokens or 0), 0) / 1000.0, CONTEXT_CAP_CU)
    cu_llm = float(max(int(llm_calls or 0), 0))
    cu_tools = 0.0
    for tool_name, toolkit_type, count in tool_counts or ():
        cu_tools += tool_weight(weights, tool_name, toolkit_type) * int(count or 0)
    return {
        "cu_context": round(cu_context, 3),
        "cu_llm": cu_llm,
        "cu_tools": round(cu_tools, 3),
        "cu_total": round(cu_context + cu_llm + cu_tools, 3),
    }


def label_for(cu, thresholds):
    """L/M/H/X for a CU value under the given thresholds."""
    if cu >= thresholds["extreme"]:
        return "X"
    if cu >= thresholds["high"]:
        return "H"
    if cu >= thresholds["medium"]:
        return "M"
    return "L"


def _valid(thresholds):
    values = [thresholds.get(key) for key in THRESHOLD_KEYS]
    return all(isinstance(v, (int, float)) and v > 0 for v in values) \
        and values[0] < values[1] < values[2]


def normalize_thresholds(value, fallback=None):
    """{medium, high, extreme} as floats, or fallback when missing or not strictly increasing."""
    fallback = dict(fallback or DEFAULT_THRESHOLDS)
    if not isinstance(value, dict):
        return fallback
    try:
        thresholds = {key: float(value[key]) for key in THRESHOLD_KEYS}
    except (KeyError, TypeError, ValueError):
        return fallback
    return thresholds if _valid(thresholds) else fallback


def resolve_thresholds(calibrated, sample_runs, sample_entities, config):
    """(thresholds, source): calibrated quantiles once the sample is big enough, pins last."""
    thresholds, source = normalize_thresholds(config.get("default_thresholds")), "default"
    calibration = config.get("calibration") or {}
    enough = sample_runs >= int(calibration.get("min_runs", 200)) \
        and sample_entities >= int(calibration.get("min_entities", 10))
    if enough and calibrated:
        candidate = {key: round(float(v), 1) for key, v in zip(THRESHOLD_KEYS, calibrated)}
        if _valid(candidate):
            thresholds, source = candidate, "calibrated"
    #
    pinned = {}
    for key, value in (config.get("pinned_thresholds") or {}).items():
        if key in THRESHOLD_KEYS and value is not None:
            pinned[key] = float(value)
    candidate = {**thresholds, **pinned}
    # An inconsistent pin (e.g. high below calibrated medium) is ignored rather than mislabel
    if pinned and _valid(candidate):
        return candidate, "pinned"
    return thresholds, source
