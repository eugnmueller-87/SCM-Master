"""Import every model module so SQLAlchemy registers all tables on ``Base``.

Import order matters only for readability here; relationships resolve by name.
"""
from app.models.auth import (  # noqa: F401
    Role,
    User,
)
from app.models.catalog import (  # noqa: F401
    ContractDocument,
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
from app.models.decision import (  # noqa: F401
    DecisionLog,
)
from app.models.flow import (  # noqa: F401
    DEPLOYABLE_STATUSES,
    GONE_STATUSES,
    IN_USE_STATUSES,
    WAREHOUSE_STATUSES,
    Asset,
    AssetEvent,
    AssetEventType,
    AssetStatus,
    Location,
    LocationType,
    Receipt,
    ReceiptItem,
)
from app.models.kpi import (  # noqa: F401
    FleetMilestone,
    KpiSnapshot,
    KpiTarget,
)
from app.models.ordering import (  # noqa: F401
    Package,
    PackageLine,
)
from app.models.procurement import (  # noqa: F401
    OrderItem,
    OrderStatus,
    PurchaseOrder,
)
from app.models.rental import (  # noqa: F401
    ContractStatus,
    RentalContract,
)
from app.models.requisition import (  # noqa: F401
    PurchaseRequisition,
    RequisitionFeedback,
    RequisitionLine,
    RequisitionStatus,
)
from app.models.simulation import (  # noqa: F401
    WorldClock,
)
from app.models.tco import (  # noqa: F401
    DeploymentCost,
    DeploymentTask,
    DepreciationMethod,
    EolCost,
    LandedCost,
    LandedCostType,
    OpexLedger,
    RecoveryValue,
    ServiceEvent,
    ServiceKind,
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
    "ContractDocument",
    "KpiTarget",
    "KpiSnapshot",
    "FleetMilestone",
    "RentalContract",
    "ContractStatus",
    "WAREHOUSE_STATUSES",
    "IN_USE_STATUSES",
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
    "DecisionLog",
    "TrkSupplier",
    "TrkPurchaseOrder",
    "Shipment",
    "ShipmentEvent",
    "LandedCost",
    "LandedCostType",
    "DeploymentCost",
    "DeploymentTask",
    "OpexLedger",
    "EolCost",
    "RecoveryValue",
    "DepreciationMethod",
    "ServiceEvent",
    "ServiceKind",
    "WorldClock",
]
