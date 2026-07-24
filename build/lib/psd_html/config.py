"""The emit-size / author-size knobs, in a config file (not just a CLI flag).

An email LAYS OUT narrow (~600-640 CSS px) -- that is the email medium. Fidelity on high-resolution
screens comes from AUTHORING larger (e.g. 2x ~ 1280 px) and shipping the image assets at that full
resolution while DISPLAYING them at the CSS width. The ratio of the two is the `density`:

    density = <PSD canvas width> / email_width      (e.g. a 1280px PSD emitted at 640 -> density 2)

so a designer authors at whatever resolution they like and the tool scales the layout to the target
email width while keeping the pictures crisp. This module loads/validates the config and resolves
the density for a given PSD, honoring CLI overrides.

Config file (`psd_html.config.json`, JSON):

    {
      "email_width": 640,     // the size the email is EMITTED at (target CSS width). > 0. Default 640.
      "author_width": 1280,   // optional: the width designers AUTHOR at. Validates the PSD matches.
      "density": null          // optional HARD override; when set, wins over the width-derived value.
    }

DENSITY RESOLUTION (highest precedence first), given the PSD's actual canvas width `W`:
    1. cli_density               -> that value
    2. cli_email_width E         -> W / E
    3. config["density"]         -> that value
    4. config["email_width"] E   -> W / E     (the normal path)
    5. default                   -> 1.0

Fail-loud: a malformed config (email_width <= 0, non-numeric field, unreadable JSON) raises
ConfigError at load naming the field -- never a silent default. An author_width that does not match
the PSD canvas returns a loud warning (the caller surfaces it), never a silent mis-scale.

SAFETY: every f-string / .format in this module builds a human-readable error or warning MESSAGE.
There is no SQL, shell, or any other interpreter/sink here -- this module only reads a local JSON
config and does arithmetic. (Noted for the repo security-reminder hook, which pattern-matches
f-strings.)
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from typing import Optional, Tuple

DEFAULT_CONFIG_FILENAME = "psd_html.config.json"
DEFAULT_EMAIL_WIDTH = 640
# How far the PSD canvas width may sit from a declared author_width before it is flagged (px).
_AUTHOR_WIDTH_TOLERANCE = 2


class ConfigError(ValueError):
    """A config file is malformed (bad JSON, missing/invalid field). Raised at load -- fail loud."""


@dataclass
class Config:
    email_width: int = DEFAULT_EMAIL_WIDTH
    author_width: Optional[int] = None
    density: Optional[float] = None

    def __post_init__(self) -> None:
        # Enforce the "positive finite number" invariant on the TYPE, not just in load_config():
        # a directly-constructed Config(density=-1) (or Config.defaults() then a bad reassignment
        # feeding resolve_density) otherwise flows an inverted/zero scale straight into the layout
        # math. Validation + coercion happen here so every construction path is covered; direct
        # post-construction mutation is additionally re-checked in resolve_density().
        self.email_width = _require_positive_number(self.email_width, "email_width", integer=True)
        if self.author_width is not None:
            self.author_width = _require_positive_number(self.author_width, "author_width", integer=True)
        if self.density is not None:
            self.density = _require_positive_number(self.density, "density", integer=False)

    @staticmethod
    def defaults() -> "Config":
        return Config()


def _require_positive_number(value, field: str, *, integer: bool):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"config field {field!r} must be a positive number, got {value!r}")
    if not math.isfinite(value) or value <= 0:
        raise ConfigError(f"config field {field!r} must be > 0, got {value!r}")
    return int(value) if integer else float(value)


def load_config(path: Optional[str] = None) -> Config:
    """Load + validate a config from `path`; if omitted, use `psd_html.config.json` in the current
    working directory if present, else built-in defaults. Fail loud (ConfigError) on a malformed
    file; a missing default file is NOT an error (returns defaults)."""
    resolved = path or (DEFAULT_CONFIG_FILENAME if os.path.isfile(DEFAULT_CONFIG_FILENAME) else None)
    if resolved is None:
        return Config.defaults()
    if not os.path.isfile(resolved):
        raise ConfigError(f"config file not found: {resolved}")
    try:
        with open(resolved, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"could not read config {resolved!r}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"config {resolved!r} must be a JSON object, got {type(raw).__name__}")

    cfg = Config.defaults()
    if "email_width" in raw and raw["email_width"] is not None:
        cfg.email_width = _require_positive_number(raw["email_width"], "email_width", integer=True)
    if raw.get("author_width") is not None:
        cfg.author_width = _require_positive_number(raw["author_width"], "author_width", integer=True)
    if raw.get("density") is not None:
        cfg.density = _require_positive_number(raw["density"], "density", integer=False)
    return cfg


def resolve_density(
    config: Config,
    canvas_width: int,
    *,
    cli_density: Optional[float] = None,
    cli_email_width: Optional[int] = None,
) -> Tuple[float, str, Optional[str]]:
    """Resolve the effective density for a PSD of width `canvas_width`, per the precedence in the
    module docstring. Returns `(density, source, warning_or_None)` -- `source` is a short tag
    ("cli:--density" / "config:email_width" / "default" ...) so a run reports what scale it used;
    `warning` is set only for an author_width mismatch (loud, never silent)."""
    warning = None
    if config.author_width is not None and abs(int(canvas_width) - int(config.author_width)) > _AUTHOR_WIDTH_TOLERANCE:
        warning = (
            f"PSD authored at {int(canvas_width)}px but config author_width is {int(config.author_width)}px "
            "-- density is derived from the ACTUAL canvas width, so verify this is intended"
        )

    if cli_density is not None:
        if isinstance(cli_density, bool) or not isinstance(cli_density, (int, float)) \
                or not math.isfinite(cli_density) or cli_density <= 0:
            raise ConfigError(f"--density must be a finite number > 0, got {cli_density!r}")
        return float(cli_density), "cli:--density", warning
    if cli_email_width is not None:
        return _density_from_width(canvas_width, cli_email_width), "cli:--email-width", warning
    if config.density is not None:
        # Re-validate exactly like the cli_density branch above -- __post_init__ guards
        # construction, but a post-construction `cfg.density = 0` mutation would otherwise reach
        # here unchecked and scale the whole layout by a zero/negative factor.
        if isinstance(config.density, bool) or not isinstance(config.density, (int, float)) \
                or not math.isfinite(config.density) or config.density <= 0:
            raise ConfigError(f"config density must be a finite number > 0, got {config.density!r}")
        return float(config.density), "config:density", warning
    if config.email_width:
        return _density_from_width(canvas_width, config.email_width), "config:email_width", warning
    return 1.0, "default", warning


def _density_from_width(canvas_width: int, email_width: int) -> float:
    if email_width <= 0:
        raise ConfigError(f"email_width must be > 0 to derive density, got {email_width!r}")
    return float(canvas_width) / float(email_width)
