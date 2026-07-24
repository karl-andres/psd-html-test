"""Tests for config.py: load/validate + density resolution precedence + fail-loud."""
from __future__ import annotations

import json

import pytest

from psd_html.config import Config, ConfigError, load_config, resolve_density


def _write(path, obj):
    path.write_text(json.dumps(obj), encoding="utf-8")
    return str(path)


def test_defaults_when_no_file():
    cfg = load_config(None)  # no explicit path; test cwd has no psd_html.config.json
    assert cfg.email_width == 640
    assert cfg.author_width is None
    assert cfg.density is None


def test_load_valid_file(tmp_path):
    p = _write(tmp_path / "c.json", {"email_width": 600, "author_width": 1200, "density": None})
    cfg = load_config(p)
    assert cfg.email_width == 600 and cfg.author_width == 1200 and cfg.density is None


def test_bad_email_width_fails_loud(tmp_path):
    for bad in (0, -5, "wide", True):
        p = _write(tmp_path / "c.json", {"email_width": bad})
        with pytest.raises(ConfigError):
            load_config(p)


def test_nan_infinity_density_fails_loud(tmp_path):
    # json.load parses NaN/Infinity by default, so a non-finite density would otherwise load
    # silently and defeat the fail-loud contract (nan <= 0 and inf <= 0 are both False).
    for bad in (float("nan"), float("inf"), float("-inf")):
        p = _write(tmp_path / "c.json", {"density": bad})
        with pytest.raises(ConfigError):
            load_config(p)


def test_missing_explicit_config_fails_loud(tmp_path):
    with pytest.raises(ConfigError):
        load_config(str(tmp_path / "nope.json"))


def test_non_object_config_fails_loud(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("[1,2,3]", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(str(p))


def test_cwd_discovery_loads_default_config_file(tmp_path, monkeypatch):
    # The normal production path: no explicit path -> pick up ./psd_html.config.json. Only the
    # file-ABSENT case was covered; this locks the file-PRESENT discovery branch.
    (tmp_path / "psd_html.config.json").write_text(json.dumps({"email_width": 600}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert load_config(None).email_width == 600


def test_malformed_json_raises_configerror_not_raw_decode_error(tmp_path):
    # Locks the documented fail-loud contract: a syntactically broken file surfaces ConfigError,
    # never a raw json.JSONDecodeError (guards against narrowing `except (OSError, JSONDecodeError)`).
    p = tmp_path / "c.json"
    p.write_text("{oops", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(str(p))


def test_author_width_and_density_are_validated(tmp_path):
    # author_width shares the integer path; density exercises the integer=False branch that
    # email_width never hits. Both must fail loud on non-positive / non-numeric input.
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path / "a.json", {"author_width": 0}))
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path / "d.json", {"density": "big"}))
    # a valid fractional density loads via the integer=False branch
    assert load_config(_write(tmp_path / "ok.json", {"density": 2.5})).density == 2.5


# --- density resolution precedence (canvas width W=1280) ------------------------------------------


def test_cli_density_wins():
    cfg = Config(email_width=640, author_width=None, density=3.0)
    d, source, _ = resolve_density(cfg, 1280, cli_density=5.0, cli_email_width=320)
    assert d == 5.0 and source == "cli:--density"


def test_cli_density_zero_or_negative_fails_loud():
    # cli_density must be finite and > 0, consistent with config.density validation; a degenerate
    # --density (0, negative, or non-finite) must fail loud rather than yield a broken scale.
    cfg = Config(email_width=640, density=None)
    for bad in (0, 0.0, -1.0, float("nan"), float("inf")):
        with pytest.raises(ConfigError):
            resolve_density(cfg, 1280, cli_density=bad)


def test_cli_email_width_beats_config():
    cfg = Config(email_width=640, density=3.0)
    d, source, _ = resolve_density(cfg, 1280, cli_email_width=640)
    assert d == 2.0 and source == "cli:--email-width"


def test_config_density_beats_config_email_width():
    cfg = Config(email_width=640, density=3.0)
    d, source, _ = resolve_density(cfg, 1280)
    assert d == 3.0 and source == "config:density"


def test_config_email_width_is_the_normal_path():
    cfg = Config(email_width=640, density=None)
    d, source, _ = resolve_density(cfg, 1280)
    assert d == 2.0 and source == "config:email_width"


def test_author_width_mismatch_warns_but_still_resolves():
    cfg = Config(email_width=640, author_width=1280, density=None)
    d, _source, warning = resolve_density(cfg, 900)  # canvas != author_width
    assert d == pytest.approx(900 / 640)
    assert warning is not None and "author_width" in warning


def test_author_width_match_no_warning():
    cfg = Config(email_width=640, author_width=1280, density=None)
    _d, _source, warning = resolve_density(cfg, 1280)
    assert warning is None
