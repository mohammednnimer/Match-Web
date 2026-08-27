"""Table registry.

Every table the admin panel manages is described once here; routes.py builds
all CRUD SQL from these descriptions. Column names are never taken from user
input directly -- sort keys and payload keys are matched against these
whitelists before they reach a query.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Tuple

AUDIT_COLUMNS = ("created_at", "updated_at", "created_by")


@dataclass(frozen=True)
class Reference:
    """A payload key that must be resolved to a foreign key before writing.

    e.g. the UI sends {"company_name": "أكاديمية النور"}; we look up
    clients.id WHERE name_ar = that value and write {"client_id": 4}.
    """

    table: str
    lookup_column: str
    fk_column: str


@dataclass(frozen=True)
class Entity:
    table: str
    writable: Tuple[str, ...]
    search: Tuple[str, ...]
    sortable: Tuple[str, ...]
    label_column: str = "name_en"
    filterable: Tuple[str, ...] = ("status",)
    read_only: bool = False
    select_extra: str = ""
    join_clause: str = ""
    references: Dict[str, Reference] = field(default_factory=dict)
    # Friendly payload keys the UI sends, mapped onto real column names.
    aliases: Dict[str, str] = field(default_factory=dict)
    # Plaintext payload keys that must be hashed before they reach a column.
    hash_fields: Dict[str, str] = field(default_factory=dict)
    # Columns never included in an API response.
    hidden: Tuple[str, ...] = ()
    # False for tables that have no created_by column.
    stamps_creator: bool = True

    @property
    def select_columns(self) -> str:
        base = f"{self.table}.*"
        return f"{base}, {self.select_extra}" if self.select_extra else base


ENTITIES: Dict[str, Entity] = {
    "clients": Entity(
        table="clients",
        writable=("name_ar", "name_en", "sector", "site", "status", "logo_url"),
        aliases={"name": "name_ar", "category": "sector", "logo": "logo_url"},
        search=("name_ar", "name_en", "site"),
        sortable=("id", "name_ar", "name_en", "site", "sector", "status", "created_at"),
        label_column="name_ar",
        filterable=("sector", "status"),
    ),
    "feedback": Entity(
        table="feedback",
        # sector is derived from the parent client by a DB trigger.
        writable=(
            "name_ar", "name_en", "role_ar", "role_en",
            "body_ar", "body_en", "rating", "status", "client_id",
        ),
        aliases={"client_name": "name_ar", "job_title": "role_ar", "feedback": "body_ar"},
        search=("name_ar", "name_en", "role_ar", "role_en", "body_ar", "body_en"),
        sortable=("id", "name_ar", "name_en", "rating", "status", "created_at"),
        filterable=("sector", "status"),
        select_extra="clients.site AS client_site, clients.name_ar AS company_name",
        join_clause="JOIN clients ON clients.id = feedback.client_id",
        references={
            "company_name": Reference("clients", "name_ar", "client_id"),
            "client_site": Reference("clients", "site", "client_id"),
        },
    ),
    "users": Entity(
        table="users",
        # `password` arrives as plaintext, is bcrypt-hashed into the password
        # column, and is never returned by the API.
        writable=("name", "email", "password"),
        hash_fields={"password": "password"},
        hidden=("password",),
        search=("name", "email"),
        sortable=("id", "name", "email", "created_at"),
        label_column="email",
        filterable=(),
        stamps_creator=False,
    ),
    "portals": Entity(
        table="portals",
        writable=("code", "name_ar", "name_en", "roles", "user_count", "status"),
        search=("code", "name_ar", "name_en"),
        sortable=("id", "code", "name_ar", "name_en", "roles", "user_count", "status", "created_at"),
        stamps_creator=False,
        label_column="code",
    ),
    "subscriptions": Entity(
        table="subscriptions",
        writable=("plan", "academic_year", "seats", "ends_at", "status", "client_id"),
        search=("academic_year",),
        sortable=("id", "academic_year", "plan", "seats", "ends_at", "status", "created_at"),
        label_column="academic_year",
        select_extra="clients.name_ar AS company_name",
        join_clause="JOIN clients ON clients.id = subscriptions.client_id",
        references={
            "company_name": Reference("clients", "name_ar", "client_id"),
        },
    ),
    "modules": Entity(
        table="modules",
        writable=("code", "name_ar", "name_en", "scope", "client_count", "status"),
        search=("code", "name_ar", "name_en"),
        sortable=("id", "code", "name_ar", "name_en", "scope", "client_count", "status", "created_at"),
        stamps_creator=False,
        label_column="code",
    ),
    "stats": Entity(
        table="stats",
        writable=("label_ar", "label_en", "value", "suffix", "icon", "sort_order", "status", "is_visible"),
        aliases={"label": "label_ar"},
        filterable=("status", "is_visible"),
        search=("key", "label_ar", "label_en"),
        sortable=("id", "key", "label_ar", "value", "sort_order", "status", "created_at"),
        label_column="key",
    ),
    "images": Entity(
        table="site_images",
        writable=("slot", "title", "category", "image_url", "alt_text", "sort_order", "is_visible"),
        aliases={"url": "image_url", "alt": "alt_text"},
        search=("slot", "title", "category", "image_url"),
        sortable=("id", "slot", "title", "category", "sort_order", "created_at"),
        filterable=("category", "is_visible"),
        label_column="title",
    ),
    "demo_requests": Entity(
        table="demo_requests",
        writable=("full_name", "email", "phone_number", "company_name",
                  "sector", "message", "status", "handled_by"),
        aliases={"name": "full_name", "phone": "phone_number", "company": "company_name"},
        search=("full_name", "email", "phone_number", "company_name", "sector", "message"),
        sortable=("id", "full_name", "email", "company_name", "sector", "status", "created_at"),
        filterable=("status", "sector"),
        label_column="full_name",
        stamps_creator=False,
    ),
    "logs": Entity(
        table="logs",
        writable=(),
        search=("actor", "action", "entity", "target"),
        sortable=("id", "actor", "action", "entity", "at"),
        filterable=("level",),
        label_column="target",
        read_only=True,
    ),
}


def get_entity(name: str) -> Entity | None:
    return ENTITIES.get(name)


# Tables holding personal data: excluded from the public list route and
# reachable only through their own authenticated endpoints.
PRIVATE_TABLES = frozenset({"demo_requests", "users", "logs"})
