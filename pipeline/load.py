"""Strict CSV loading: header contract, typed parsing, input hash, source lines."""

from typing import BinaryIO

from contracts.config import InputConfig
from contracts.runtime import LoadedData


def load_csv(source: BinaryIO, config: InputConfig) -> LoadedData:
    """Hash the raw bytes, then parse the 17-column export without coercion.

    Supply ID is read as an opaque string; dates parse strictly; invalid
    numerics are never replaced with zero. Size and row bounds are enforced
    before expensive parsing.
    """
    raise NotImplementedError
