from decimal import Decimal

from pydantic import BaseModel, Field, field_validator


class SendRequest(BaseModel):
    to_address: str = Field(
        ...,
        description="TRON address (T...)",
        min_length=34,
        max_length=34,
    )
    amount: Decimal = Field(..., gt=0, description="Amount in USDT (e.g. 245.0572)")
    idempotency_key: str = Field(
        ...,
        description="Unique key: wallet + date (e.g. TAJ9cefSdh4jZs83hQyNi9mtVuDnGEDKJ62026-04-03)",
        min_length=1,
        max_length=128,
    )
    # Optional: explicit source wallet name (must match one configured at
    # startup). When omitted, the service auto-picks the wallet with the
    # largest USDT balance — see WalletPool.resolve. Single-wallet
    # deployments can ignore this field entirely.
    wallet: str | None = Field(
        default=None,
        description="Source wallet name. Omit for auto-pick by max USDT balance.",
        max_length=64,
    )

    @field_validator("amount")
    @classmethod
    def amount_max_decimals(cls, v: Decimal) -> Decimal:
        if v.as_tuple().exponent < -6:
            raise ValueError("USDT supports max 6 decimal places")
        return v


class SendResponse(BaseModel):
    txid: str
    from_address: str
    # Echo back the wallet name that actually signed — operators correlating
    # logs across multi-wallet deploys want this.
    wallet: str
    to_address: str
    amount: str
    idempotency_key: str
    status: str = "broadcast"


class BalanceResponse(BaseModel):
    # Wallet name + address are both included so single-wallet legacy
    # consumers see what they expect, and multi-wallet consumers can
    # disambiguate.
    wallet: str
    address: str
    trx: str
    usdt: str
    # Resource visibility — operator can see at a glance whether they need
    # to top up TRX, stake more for energy, or rent energy externally.
    energy_available: int = 0
    bandwidth_free_available: int = 0
    bandwidth_paid_available: int = 0
    tron_power_staked: int = 0


class WalletSummary(BaseModel):
    """One row in the /wallets listing."""
    wallet: str
    address: str
    trx: str
    usdt: str
    energy_available: int = 0
    bandwidth_free_available: int = 0
    bandwidth_paid_available: int = 0


class WalletListResponse(BaseModel):
    """All wallets configured for this process. The ``auto_pick`` field
    points at the wallet that would handle a /send with no explicit
    ``wallet`` parameter right now (i.e. max USDT balance)."""
    wallets: list[WalletSummary]
    auto_pick: str | None = None


class HealthLiveResponse(BaseModel):
    """Lightweight liveness probe — no external calls, no auth required.

    Used for k8s/load-balancer liveness checks. Suitable to be polled
    aggressively without burning RPC calls or auth-key audit noise.
    """
    status: str = "ok"
    uptime_seconds: int


class HealthResponse(BaseModel):
    """Full readiness probe — requires auth, hits TronGrid.

    For multi-wallet deployments this returns an aggregate (no balances)
    plus a wallet count; per-wallet detail lives at /balance and /wallets.
    """
    status: str = "ok"
    network: str
    uptime_seconds: int
    shutdown_in_seconds: int
    node_connected: bool
    wallet_count: int = 0
    wallet_names: list[str] = []


class VersionResponse(BaseModel):
    """Build/version info — useful for "what's deployed?" dashboards.

    `git_sha` is captured at boot from `git rev-parse HEAD` (best effort —
    "unknown" if the working tree isn't a git checkout, e.g. inside a
    Docker image built from a tarball). `started_at` is the unix
    timestamp of process start.
    """
    git_sha: str
    started_at: float
    network: str
