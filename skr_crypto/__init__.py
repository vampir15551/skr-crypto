"""SKR Crypto — operator CLI for the SKR Crypto microservice fleet.

This package intentionally does NOT expose any code paths that move
money. The HTTP API of the underlying service is the only authorised
channel for transfers, and the CLI never proxies a ``/send``. See
``SECURITY.md``.
"""
from skr_crypto.version import __version__

__all__ = ["__version__"]
