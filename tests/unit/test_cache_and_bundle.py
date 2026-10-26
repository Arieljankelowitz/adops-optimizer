"""Cache-key completeness, per-session cache rules and private bundle integrity."""

import dataclasses
import importlib.util
import io
import json
import zipfile
from decimal import Decimal
from pathlib import Path

import pytest

from contracts.config import AnalyticsConfig, EvidenceConfig, RunConfig
from contracts.enums import RunStatus
from llm.profiles import load_profiles
from report.bundle import (
    BUNDLE_FILES,
    MANIFEST_NAME,
    BundleError,
    as_cached,
    bundle_zip,
    from_bundle,
    read_bundle,
    to_bundle,
    write_bundle,
)
from runner import DEMO_RUN_PATH, CacheKey, RunCache, cache_key, input_sha256, load_demo_run

REPO_ROOT = Path(__file__).resolve().parents[2]
PLAN_KEY = ("input_sha256", "schema_version", "analytics_config_hash", "model_profile_hash",
            "prompt_hash", "tool_version", "assumption_hash")


def _builder():
    spec = importlib.util.spec_from_file_location(
        "build_demo_run", REPO_ROOT / "scripts" / "build_demo_run.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BUILDER = _builder()


@pytest.fixture(scope="module")
def final():
    return BUILDER.build_demo_outcome(RunStatus.FINAL)


def test_cache_key_has_exactly_the_plan_fields() -> None:
    assert tuple(f.name for f in dataclasses.fields(CacheKey)) == PLAN_KEY


def test_cache_key_reads_every_field_from_the_manifest(final) -> None:
    m = final.artifact.manifest
    key = CacheKey.from_manifest(m)
    assert all(getattr(key, name) == getattr(m, name) for name in PLAN_KEY)


@pytest.mark.parametrize("changed,expected_fields", [
    (dict(data=b"other"), {"input_sha256"}),
    (dict(config=RunConfig(analytics=AnalyticsConfig(min_requests=1))),
     {"analytics_config_hash"}),
    (dict(config=RunConfig(evidence=EvidenceConfig(test_allocation=Decimal("0.2")))),
     {"analytics_config_hash", "assumption_hash"}),
    (dict(config=RunConfig(model_profile_id=load_profiles().active_profile)),
     {"analytics_config_hash", "model_profile_hash", "prompt_hash"}),
])
def test_each_input_changes_its_key_fields(changed: dict, expected_fields: set) -> None:
    base = cache_key(input_sha256(b"data"), RunConfig())
    other = cache_key(input_sha256(changed.get("data", b"data")), changed.get("config",
                                                                              RunConfig()))
    differing = {f for f in PLAN_KEY if getattr(base, f) != getattr(other, f)}
    assert differing == expected_fields
    assert base.digest() != other.digest()


def test_profile_and_tool_versions_track_their_sources(monkeypatch, tmp_path) -> None:
    import runner

    profile_id = load_profiles().active_profile
    a = cache_key("0" * 64, RunConfig(model_profile_id=profile_id))
    assert a.model_profile_hash == load_profiles().get(profile_id).sha256()
    profiles = tmp_path / "model_profiles.yaml"
    original = runner.MODEL_PROFILES_PATH.read_text()
    profiles.write_text(original.replace("temperature: 0.2", "temperature: 0.3", 1))
    monkeypatch.setattr(runner, "MODEL_PROFILES_PATH", profiles)
    prompts = tmp_path / "prompts.py"
    prompts.write_text("PROMPT = 'changed'\n")
    monkeypatch.setattr(runner, "PROMPTS_PATH", prompts)
    tools = tmp_path / "tools.py"
    tools.write_text("VERSION = 'changed'\n")
    monkeypatch.setattr(runner, "TOOLS_PATH", tools)
    b = cache_key("0" * 64, RunConfig(model_profile_id=profile_id))
    assert a.model_profile_hash != b.model_profile_hash
    assert a.prompt_hash != b.prompt_hash and a.tool_version != b.tool_version


def test_unknown_profile_has_no_cache_key() -> None:
    with pytest.raises(ValueError):
        cache_key("0" * 64, RunConfig(model_profile_id="no_such_profile"))


def test_deterministic_runs_have_no_model_or_prompt_hash() -> None:
    key = cache_key("0" * 64, RunConfig())
    assert key.model_profile_hash is None and key.prompt_hash is None


def test_cache_hits_only_on_the_complete_key(final) -> None:
    cache = RunCache()
    cache.put(final)
    key = CacheKey.from_manifest(final.artifact.manifest)
    hit = cache.get(key)
    assert hit is not None and hit.status is RunStatus.CACHED_PREVIOUS
    assert hit.artifact.manifest.generated_at == final.artifact.manifest.generated_at
    assert "CACHED PREVIOUS RUN — not fresh inference." in hit.report.markdown
    for name in PLAN_KEY:
        miss = dataclasses.replace(key, **{name: "f" * 64 if name != "schema_version" else "2"})
        assert cache.get(miss) is None, name


def test_cache_rejects_a_contaminated_entry(final) -> None:
    cache = RunCache()
    cache.put(final)
    key = CacheKey.from_manifest(final.artifact.manifest)
    other_input = final.artifact.manifest.model_copy(update={"input_sha256": "e" * 64})
    tampered = dataclasses.replace(
        final, artifact=final.artifact.model_copy(update={"manifest": other_input}))
    cache._entries[key] = tampered  # an entry whose content belongs to another file
    assert cache.get(key) is None


def test_cache_is_bounded() -> None:
    cache = RunCache(max_entries=2)
    outcomes = [BUILDER.build_demo_outcome(s) for s in (
        RunStatus.FINAL, RunStatus.PARTIAL, RunStatus.DETERMINISTIC_ONLY)]
    for o in outcomes:
        cache.put(o)
    assert len(cache) == 2


def test_bundle_round_trip(final, tmp_path) -> None:
    target = write_bundle(final, tmp_path)
    assert sorted(p.name for p in target.iterdir()) == sorted((MANIFEST_NAME, *BUNDLE_FILES))
    restored = read_bundle(target)
    assert restored.artifact == final.artifact
    assert restored.report == final.report
    assert restored.run.validation_failures == final.run.validation_failures
    with pytest.raises(FileExistsError):
        write_bundle(final, tmp_path)


def test_tampered_bundle_is_refused(final, tmp_path) -> None:
    target = write_bundle(final, tmp_path)
    facts = json.loads((target / "facts.json").read_text())
    facts[0]["value"] = "999999"
    (target / "facts.json").write_text(json.dumps(facts))
    with pytest.raises(BundleError, match="facts.json"):
        read_bundle(target)


def test_incomplete_bundle_is_refused(final, tmp_path) -> None:
    target = write_bundle(final, tmp_path)
    (target / "report.md").unlink()
    with pytest.raises(BundleError, match="missing"):
        read_bundle(target)


def test_old_cache_without_new_planning_facts_is_renderable_without_inference():
    from contracts.runtime import FactRegistry
    from report.summary import ShortReport, audit_summary

    outcome = BUILDER.build_demo_outcome(RunStatus.FINAL)
    b = outcome.run.bundle
    historical = FactRegistry(b.run_namespace, [f for f in b.facts.values()
                              if not f.provenance.formula_id.startswith("report_")])
    old = dataclasses.replace(outcome, run=dataclasses.replace(outcome.run,
                              bundle=dataclasses.replace(b, facts=historical)))
    cached = as_cached(old)
    assert cached.run.manifest == outcome.run.manifest
    assert cached.status is RunStatus.CACHED_PREVIOUS
    assert "extra profit per week if" in cached.report.markdown
    assert audit_summary(ShortReport(cached.report, cached.html), cached.run).ok


def test_cached_relabel_keeps_bundle_self_consistent() -> None:
    cached = BUILDER.build_demo_outcome(RunStatus.CACHED_PREVIOUS)
    restored = from_bundle(to_bundle(cached))  # re-verifies every file hash
    assert restored.status is RunStatus.CACHED_PREVIOUS
    assert restored.report == cached.report


def test_zip_contains_the_bundle(final) -> None:
    with zipfile.ZipFile(io.BytesIO(bundle_zip(final))) as zf:
        names = {Path(n).name for n in zf.namelist()}
    assert names == {MANIFEST_NAME, *BUNDLE_FILES}


def test_committed_demo_fixture_matches_the_builder() -> None:
    stale = [str(p.relative_to(REPO_ROOT)) for p, text in BUILDER.rendered_files().items()
             if not p.exists() or p.read_text(encoding="utf-8") != text]
    assert not stale, f"rerun scripts/build_demo_run.py: {stale}"


def test_demo_run_loads_as_a_labelled_cached_run() -> None:
    demo = load_demo_run()
    assert DEMO_RUN_PATH.is_file()
    assert demo.status is RunStatus.CACHED_PREVIOUS
    assert demo.artifact.manifest.model_id == "synthetic-demo-model"
    assert demo.metrics is None
