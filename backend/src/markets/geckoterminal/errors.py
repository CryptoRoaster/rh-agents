"""Fixed safe codes; upstream responses and exceptions never escape the boundary."""


class ProviderError(Exception):
    code = "provider_error"

    def __init__(self) -> None:
        super().__init__(self.code)


class ConfigurationError(ProviderError):
    code = "provider_configuration"


class UnavailableError(ProviderError):
    code = "provider_unavailable"


class ConnectivityError(UnavailableError):
    code = "provider_connectivity"


class RateLimitError(ProviderError):
    code = "provider_rate_limited"


class ClientError(ProviderError):
    code = "provider_client_error"


class AuthenticationError(ProviderError):
    code = "provider_authentication"


class ContractError(ProviderError):
    code = "provider_contract"


class IdentityError(ContractError):
    code = "provider_identity"


class UnsupportedNetworkError(ProviderError):
    code = "unsupported_network"


class BudgetError(ProviderError):
    code = "request_budget_exhausted"
