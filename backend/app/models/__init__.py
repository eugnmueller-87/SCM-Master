"""Import every model module so SQLAlchemy registers all tables on ``Base``.

Import order matters only for readability here; relationships resolve by name.
"""
from app.models.catalog import (  # noqa: F401
    Organization,
    Product,
    ProductSupplier,
)
from app.models.procurement import (  # noqa: F401
    PurchaseOrder,
    OrderItem,
    OrderStatus,
)
from app.models.flow import (  # noqa: F401
    Location,
    LocationType,
    Receipt,
    ReceiptItem,
    Asset,
    AssetStatus,
    AssetEvent,
    AssetEventType,
)

__all__ = [
    "Organization",
    "Product",
    "ProductSupplier",
    "PurchaseOrder",
    "OrderItem",
    "OrderStatus",
    "Location",
    "LocationType",
    "Receipt",
    "ReceiptItem",
    "Asset",
    "AssetStatus",
    "AssetEvent",
    "AssetEventType",
]
