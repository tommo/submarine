"""L2 backend: JSON-RPC client, specs, providers, grok/kimi helpers. Sublime-free."""
from . import grok, kimi, providers, rpc, specs
from .rpc import JsonRpcClient
from .specs import (
    BackendSpec,
    abbrev_for,
    all_backends,
    default_models_dict,
    get,
    is_available,
)

__all__ = [
    "JsonRpcClient",
    "BackendSpec",
    "abbrev_for",
    "all_backends",
    "default_models_dict",
    "get",
    "grok",
    "is_available",
    "kimi",
    "providers",
    "rpc",
    "specs",
]
