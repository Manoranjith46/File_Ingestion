"""Configuration settings for Auth0 Identity Provider integration."""

from helpers.get_env import get_env


class Auth0Settings:
    """Hold Auth0 OIDC configuration variables."""

    def __init__(self):
        self._domain = None
        self._audience = None
        self._issuer = None
        self._client_id = None
        self._client_secret = None

    @property
    def domain(self) -> str:
        """Return the Auth0 domain (e.g. dev-xyz.us.auth0.com)."""
        if self._domain is not None:
            return self._domain.strip().rstrip("/")
        return get_env("AUTH0_DOMAIN", default="", required=False).strip().rstrip("/")

    @domain.setter
    def domain(self, val: str) -> None:
        self._domain = val

    @property
    def audience(self) -> str:
        """Return the Auth0 API audience identifier."""
        if self._audience is not None:
            return self._audience.strip()
        return get_env("AUTH0_AUDIENCE", default="", required=False).strip()

    @audience.setter
    def audience(self, val: str) -> None:
        self._audience = val

    @property
    def issuer(self) -> str:
        """Return the trusted token issuer URL."""
        if self._issuer is not None:
            return self._issuer.rstrip("/") + "/"
        configured_issuer = get_env("AUTH0_ISSUER", default="", required=False).strip()
        if configured_issuer:
            return configured_issuer.rstrip("/") + "/"
        if self.domain:
            return f"https://{self.domain}/"
        return ""

    @issuer.setter
    def issuer(self, val: str) -> None:
        self._issuer = val

    @property
    def client_id(self) -> str:
        """Return the Auth0 backend application client ID."""
        if self._client_id is not None:
            return self._client_id.strip()
        return get_env("AUTH0_CLIENT_ID", default="", required=False).strip()

    @client_id.setter
    def client_id(self, val: str) -> None:
        self._client_id = val

    @property
    def client_secret(self) -> str:
        """Return the Auth0 backend application client secret."""
        if self._client_secret is not None:
            return self._client_secret.strip()
        return get_env("AUTH0_CLIENT_SECRET", default="", required=False).strip()

    @client_secret.setter
    def client_secret(self, val: str) -> None:
        self._client_secret = val

    @property
    def jwks_url(self) -> str:
        """Return the JWKS endpoint URL for the configured domain."""
        if not self.domain:
            return ""
        return f"https://{self.domain}/.well-known/jwks.json"

    @property
    def is_configured(self) -> bool:
        """Return True when minimum required Auth0 parameters are present."""
        return bool(self.domain and self.audience)


auth0_settings = Auth0Settings()
