from sqlalchemy import JSON, text
from sqlalchemy.dialects.postgresql import JSONB


JSON_VARIANT = JSON().with_variant(JSONB, "postgresql")
EMPTY_JSON_OBJECT = text("'{}'")
EMPTY_JSON_ARRAY = text("'[]'")