from pathlib import Path

import pytest

from crawlbudget.config import load_config
from crawlbudget.errors import (
    CFG_DUPLICATE_JOB, CFG_DUPLICATE_SITE, CFG_INVALID_URL, CFG_INVALID_YAML,
    CFG_MISSING_FIELD, CFG_NON_LOOPBACK_TARGET, CFG_NON_POSITIVE_LIMIT, CFG_UNKNOWN_SITE,
    ConfigError,
)
from tests.conftest import FIXTURES, ROOT


def test_demo_configuration_shape_and_stable_fingerprints(demo_config, copy_fixture_tree):
    assert set(demo_config.sites) == {"mock-alpha", "mock-beta", "mock-gamma"}
    assert len(demo_config.jobs) == 3
    assert demo_config.sites["mock-beta"].max_retries == 2
    config_fp = demo_config.config_fingerprint
    seeds_fp = demo_config.seeds_fingerprint
    robots_fp = demo_config.robots_fingerprint
    assert len(config_fp) == len(seeds_fp) == len(robots_fp) == 64
    copied = copy_fixture_tree()
    duplicate = load_config(copied / "demo_sites.yaml", project_root=copied)
    assert (duplicate.config_fingerprint, duplicate.seeds_fingerprint,
            duplicate.robots_fingerprint) == (config_fp, seeds_fp, robots_fp)
    seed = copied / "seeds" / "mock-alpha_urls.txt"
    seed.write_text(seed.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
    changed = load_config(copied / "demo_sites.yaml", project_root=copied)
    assert changed.seeds_fingerprint != seeds_fp


def test_duplicate_and_aggregated_validation_errors(config_factory, tmp_path):
    def duplicates(raw):
        raw["sites"].append(dict(raw["sites"][0]))
        raw["jobs"].append(dict(raw["jobs"][0]))
    with pytest.raises(ConfigError) as error:
        config_factory(duplicates)
    assert {issue.code for issue in error.value.issues} >= {CFG_DUPLICATE_SITE, CFG_DUPLICATE_JOB}
    def broken(raw):
        site = raw["sites"][0]
        site["base_url"] = "http://example.invalid:9000/mock-alpha/"
        site["daily_quota"] = 0
        site.pop("burst")
        raw["jobs"][0]["site_id"] = "mock-missing"
        raw["jobs"][1]["seed_file"] = str(tmp_path / "no-such-seed.txt")
    with pytest.raises(ConfigError) as error:
        config_factory(broken)
    codes = {issue.code for issue in error.value.issues}
    assert codes >= {CFG_NON_LOOPBACK_TARGET, CFG_NON_POSITIVE_LIMIT, CFG_MISSING_FIELD,
                     CFG_UNKNOWN_SITE, "CFG_SEED_FILE_MISSING"}
    assert len(error.value.issues) >= 5


@pytest.mark.parametrize(
    "base_url",
    ["https://127.0.0.1:5021/mock-alpha/", "http://127.0.0.1:5022/mock-beta",
     "http://127.0.0.1:abc/mock-gamma/"],
)
def test_invalid_base_urls_are_rejected(config_factory, base_url):
    def mutate(raw):
        raw["sites"][0]["base_url"] = base_url
    with pytest.raises(ConfigError) as error:
        config_factory(mutate)
    assert any(issue.code == CFG_INVALID_URL for issue in error.value.issues)


@pytest.mark.parametrize("text, expected_code",
                         [("sites: [\n", CFG_INVALID_YAML), ("{}\n", CFG_MISSING_FIELD)])
def test_malformed_and_empty_yaml_are_config_errors(tmp_path, text, expected_code):
    path = tmp_path / "bad.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError) as error:
        load_config(path, project_root=tmp_path)
    assert expected_code in {issue.code for issue in error.value.issues}


def test_missing_robots_file_is_not_a_config_error(config_factory):
    def mutate(raw):
        raw["sites"][0]["robots_file"] = str(Path("/") / "does-not-exist" / "robots.txt")
    config, _path, _raw = config_factory(mutate)
    assert config.sites["mock-alpha"].robots_file.name == "robots.txt"
