"""Wire-serialization aliases shared by every router that returns money or
energy.

Postgres NUMERIC arrives as Decimal, and rule 5 forbids FLOAT for money and
energy. Serializing a Decimal as a JSON number would hand it to a JavaScript
double, which is precisely the lossy step the rule exists to prevent, so these
aliases pin serialization to str. str(Decimal) preserves the scale Postgres
sent, so 0.0000 stays "0.0000" and the client can tell a measured zero from an
absent value.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from pydantic import PlainSerializer

_as_str = PlainSerializer(str, return_type=str, when_used="json")

Money = Annotated[Decimal, _as_str]   # NUMERIC(14,4)
Energy = Annotated[Decimal, _as_str]  # NUMERIC(12,4)
Rate = Annotated[Decimal, _as_str]    # NUMERIC(10,6)
