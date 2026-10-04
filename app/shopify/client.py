from __future__ import annotations

from typing import Any

from config import Settings

try:
    from shopify_app import ShopifyApp
except ImportError:  # Allows offline tests to import the project without network installs.
    ShopifyApp = None  # type: ignore[assignment]


class ShopifyRequestError(RuntimeError):
    pass


class ShopifyClient:
    """Thin wrapper around Shopify's maintained ``shopifyapp`` GraphQL client."""

    def __init__(self, settings: Settings):
        settings.validate()
        if ShopifyApp is None:
            raise RuntimeError(
                "The 'shopifyapp' package is not installed. Run: pip3 install -r requirements.txt"
            )
        self.settings = settings
        self._app = ShopifyApp(client_id=settings.api_key, client_secret=settings.api_secret)

    def execute(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        result = self._app.admin_graphql_request(
            query,
            shop=self.settings.shop_slug,
            access_token=self.settings.access_token,
            api_version=self.settings.api_version,
            variables=variables or {},
            invalid_token_response=None,
        )
        if not result.ok:
            code = getattr(result.log, "code", "graphql_request_failed")
            detail = getattr(result.log, "detail", "Shopify GraphQL request failed.")
            raise ShopifyRequestError(f"{code}: {detail}")
        if result.data is None:
            raise ShopifyRequestError("Shopify GraphQL request returned no data.")
        return result.data
