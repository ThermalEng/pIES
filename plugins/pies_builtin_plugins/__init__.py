"""Official built-in stage plugins and catalog for pIES 1.2.0."""

from pies_builtin_plugins.catalog import (
    BUILTIN_STAGE_BY_REF,
    BUILTIN_STAGE_DESCRIPTORS,
    BUILTIN_STAGES_IMAGE,
    BuiltinStageDescriptor,
    get_builtin_stage,
    list_builtin_stages,
)

__version__ = "1.2.0"

__all__ = [
    "BUILTIN_STAGES_IMAGE",
    "BUILTIN_STAGE_BY_REF",
    "BUILTIN_STAGE_DESCRIPTORS",
    "BuiltinStageDescriptor",
    "__version__",
    "get_builtin_stage",
    "list_builtin_stages",
]
