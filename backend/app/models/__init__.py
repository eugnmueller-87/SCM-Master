"""Import every model module so SQLAlchemy registers all tables on ``Base``.

Import order matters only for readability here; relationships resolve by name.
"""
from app.models.auth import (  # noqa: F401
    Role,
    User,
)
from app.models.catalog import (  # noqa: F401
    Organization,
    Product,
    ProductSupplier,
)
from app.models.costing import (  # noqa: F401
    BOM,
    BOMLine,
    Commodity,
    CommodityPrice,
    ComponentClass,
    CostingMethod,
    CostParams,
    ShouldCostRun,
)
from app.models.flow import (  # noqa: F401
    Asset,
    AssetEvent,
    AssetEventType,
    AssetStatus,
    Location,
    LocationType,
    Receipt,
    ReceiptItem,
)
from app.models.procurement import (  # noqa: F401
    OrderItem,
    OrderStatus,
    PurchaseOrder,
)
from app.models.requisition import (  # noqa: F401
    PurchaseRequisition,
    RequisitionFeedback,
    RequisitionLine,
    RequisitionStatus,
)
from app.models.tracking import (  # noqa: F401
    Shipment,
    ShipmentEvent,
    TrkPurchaseOrder,
    TrkSupplier,
)

__all__ = [
    "Role",
    "User",
    "Organization",
    "Product",
    "ProductSupplier",
    "BOM",
    "BOMLine",
    "Commodity",
    "CommodityPrice",
    "ComponentClass",
    "CostingMethod",
    "CostParams",
    "ShouldCostRun",
    "PurchaseOrder",
    "OrderItem",
    "OrderStatus",
    "PurchaseRequisition",
    "RequisitionLine",
    "RequisitionFeedback",
    "RequisitionStatus",
    "Location",
    "LocationType",
    "Receipt",
    "ReceiptItem",
    "Asset",
    "AssetStatus",
    "AssetEvent",
    "AssetEventType",
    "TrkSupplier",
    "TrkPurchaseOrder",
    "Shipment",
    "ShipmentEvent",
]
