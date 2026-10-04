from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent
load_dotenv(ROOT_DIR / ".env")


@dataclass(frozen=True)
class Settings:
    root_dir: Path = ROOT_DIR
    shop: str = os.getenv("SHOPIFY_SHOP", "").strip()
    api_version: str = os.getenv("SHOPIFY_API_VERSION", "2026-04").strip()
    access_token: str = os.getenv("SHOPIFY_ACCESS_TOKEN", "").strip()
    api_key: str = os.getenv("SHOPIFY_API_KEY", "").strip()
    api_secret: str = os.getenv("SHOPIFY_API_SECRET", "").strip()
    query_poll_seconds: int = int(os.getenv("SHOPIFY_QUERY_POLL_SECONDS", "10"))
    mutation_poll_seconds: int = int(os.getenv("SHOPIFY_MUTATION_POLL_SECONDS", "15"))
    job_poll_seconds: int = int(os.getenv("SHOPIFY_JOB_POLL_SECONDS", "5"))

    @property
    def shop_slug(self) -> str:
        shop = self.shop.removeprefix("https://").removeprefix("http://").rstrip("/")
        return shop.removesuffix(".myshopify.com")

    @property
    def shop_domain(self) -> str:
        return f"{self.shop_slug}.myshopify.com" if self.shop_slug else ""

    @property
    def source_dir(self) -> Path:
        return self.root_dir / "source"

    @property
    def queries_dir(self) -> Path:
        return self.root_dir / "queries"

    @property
    def working_dir(self) -> Path:
        return self.root_dir / "working"

    @property
    def mutations_dir(self) -> Path:
        return self.root_dir / "mutations"

    @property
    def graphql_dir(self) -> Path:
        return self.root_dir / "graphql"

    @property
    def query_definitions_dir(self) -> Path:
        return self.graphql_dir / "queries"

    @property
    def mutation_definitions_dir(self) -> Path:
        return self.graphql_dir / "mutations"

    def validate(self) -> None:
        missing = []
        for env_name, value in (
            ("SHOPIFY_SHOP", self.shop_slug),
            ("SHOPIFY_ACCESS_TOKEN", self.access_token),
            ("SHOPIFY_API_KEY", self.api_key),
            ("SHOPIFY_API_SECRET", self.api_secret),
        ):
            if not value:
                missing.append(env_name)
        if missing:
            raise RuntimeError("Missing required .env setting(s): " + ", ".join(missing))

    def ensure_directories(self) -> None:
        for path in (
            self.source_dir,
            self.queries_dir,
            self.working_dir,
            self.mutations_dir,
            self.query_definitions_dir,
            self.mutation_definitions_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def display_path(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.root_dir.resolve()))
        except ValueError:
            return str(path)
