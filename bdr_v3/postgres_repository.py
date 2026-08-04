from __future__ import annotations

from .postgres_accounts import PostgresAccountMixin
from .postgres_base import PostgresBase
from .postgres_delivery import PostgresDeliveryMixin
from .postgres_pipeline import PostgresPipelineMixin
from .repository import BdrRepository


class PostgresRepository(
    PostgresAccountMixin,
    PostgresDeliveryMixin,
    PostgresPipelineMixin,
    PostgresBase,
    BdrRepository,
):
    """Transactional PostgreSQL implementation for the BDR v3 schema."""

    pass
