"""Frozen data contracts shared by every pipeline, model and report module.

Modules may add implementation details behind these types but must not change
their meaning silently: a breaking change bumps ``SCHEMA_VERSION`` and adds a
new ``contracts/schemas/v<N>/`` directory.
"""

SCHEMA_VERSION = "1"
