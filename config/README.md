# config

Run configuration files land here with the lanes that own them:
`quality.yaml` (quality rules), `analytics.yaml` (aggregation and detectors) and
`model_profiles.yaml` (self-hosted model profiles, loaded by `llm/profiles.py`).

Until the first two exist, their defaults live in `contracts/config.py`; the
data-quality thresholds are the `QualityConfig` defaults there. Every value is
part of the configuration hash, so changing one changes the run namespace.
