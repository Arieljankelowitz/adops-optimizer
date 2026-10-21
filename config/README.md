# config

Run configuration files land here with the lanes that own them:
`quality.yaml` (quality rules), `analytics.yaml` (aggregation and detectors) and
`model_profiles.yaml` (self-hosted model profiles).

Until then, the defaults live in `contracts/config.py`. Every value is part of
the configuration hash, so changing one changes the run namespace.
