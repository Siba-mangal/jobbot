"""Configuration loading.

Two files, deliberately separate:

- ``config/search.yaml``  — what to look for and how hard to push. Safe to commit.
- ``config/profile.yaml`` — facts about you. Gitignored, and the *only* source
  the applier is allowed to draw factual answers from.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

# Repo root — this file lives at src/jobbot/config.py
ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"


# --------------------------------------------------------------------------
# search.yaml
# --------------------------------------------------------------------------


#: Where each provider lives and which env var holds its key.
#: `base_url` empty means "native SDK, not the OpenAI-compatible path".
#: `key_env` None means the endpoint is local and needs no credential.
PROVIDERS: dict[str, dict] = {
    "anthropic": {"base_url": "", "key_env": "ANTHROPIC_API_KEY", "label": "Claude"},
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "key_env": "OPENAI_API_KEY",
        "label": "OpenAI / ChatGPT",
    },
    "gemini": {
        # Google exposes an OpenAI-compatible surface, so Gemini rides the
        # same code path as everything else rather than needing its own SDK.
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "key_env": "GEMINI_API_KEY",
        "label": "Google Gemini",
    },
    "ollama": {"base_url": "http://localhost:11434/v1", "key_env": None, "label": "Ollama (local)"},
    "lmstudio": {
        "base_url": "http://localhost:1234/v1",
        "key_env": None,
        "label": "LM Studio (local)",
    },
    "custom": {"base_url": "", "key_env": None, "label": "Custom endpoint"},
}


class ModelConfig(BaseModel):
    #: anthropic | openai | gemini | ollama | lmstudio | custom
    provider: str = "anthropic"
    scoring: str = "claude-opus-5"
    #: Overrides the provider preset. Required for `custom`; handy for pointing
    #: at a local server on a non-default port.
    base_url: str = ""
    #: Overrides which env var carries the key. Leave blank for the preset.
    api_key_env: str = ""
    batch_threshold: int = 30
    # low | medium | high | xhigh | max. Higher = better calibrated scores and
    # more tokens spent thinking. `medium` roughly halves cost if you scale up.
    effort: str = "high"

    @property
    def preset(self) -> dict:
        return PROVIDERS.get(self.provider, PROVIDERS["custom"])

    @property
    def is_native_anthropic(self) -> bool:
        """The only path with prompt caching and the Batches API."""
        return self.provider == "anthropic"

    def endpoint(self) -> str:
        """Base URL for the OpenAI-compatible path, without a trailing slash."""
        return (self.base_url or self.preset["base_url"]).rstrip("/")

    def key_env(self) -> str | None:
        """Env var holding this provider's key, or None if none is needed."""
        if self.api_key_env:
            return self.api_key_env
        return self.preset["key_env"]

    def needs_key(self) -> bool:
        """Local endpoints authenticate with nothing; hosted ones do."""
        return self.key_env() is not None


class SearchQuery(BaseModel):
    keywords: str
    location: str = ""
    remote: bool = False
    posted_within_days: int | None = None
    #: Finer-grained freshness. LinkedIn's date filter is really a
    #: seconds-ago value, so 1h and 24h windows are both expressible.
    posted_within_hours: int | None = None
    #: Shown in the UI so a run's results can be traced back to the query
    #: that produced them.
    label: str = ""

    @property
    def freshness_seconds(self) -> int | None:
        """The window as seconds, hours taking precedence over days."""
        if self.posted_within_hours:
            return self.posted_within_hours * 3600
        if self.posted_within_days:
            return self.posted_within_days * 86400
        return None

    def describe(self) -> str:
        if self.label:
            return self.label
        bits = [self.keywords or "anything"]
        if self.location:
            bits.append(f"in {self.location}")
        if self.posted_within_hours:
            bits.append(f"posted <{self.posted_within_hours}h")
        elif self.posted_within_days:
            bits.append(f"posted <{self.posted_within_days}d")
        return " ".join(bits)


class SiteConfig(BaseModel):
    enabled: bool = False
    daily_cap: int = 50
    queries: list[SearchQuery] = Field(default_factory=list)


class PrefilterConfig(BaseModel):
    exclude_title_keywords: list[str] = Field(default_factory=list)
    exclude_companies: list[str] = Field(default_factory=list)
    allow_locations: list[str] = Field(default_factory=list)
    max_years_required: int | None = None


class ReviewConfig(BaseModel):
    min_score: int = 60


class ApplyConfig(BaseModel):
    max_per_day: int = 20
    max_per_company_per_week: int = 3
    failure_circuit_breaker: int = 3


class SearchConfig(BaseModel):
    model: ModelConfig = Field(default_factory=ModelConfig)
    sites: dict[str, SiteConfig] = Field(default_factory=dict)
    prefilter: PrefilterConfig = Field(default_factory=PrefilterConfig)
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    apply: ApplyConfig = Field(default_factory=ApplyConfig)

    def enabled_sites(self) -> list[str]:
        return [name for name, cfg in self.sites.items() if cfg.enabled]


# --------------------------------------------------------------------------
# profile.yaml
# --------------------------------------------------------------------------


class Identity(BaseModel):
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    city: str = ""
    country: str = ""

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


class Links(BaseModel):
    linkedin: str = ""
    github: str = ""
    portfolio: str = ""


class Employment(BaseModel):
    current_company: str = ""
    current_title: str = ""
    total_years_experience: float = 0
    notice_period_days: int = 0
    current_ctc: str = ""
    expected_ctc: str = ""


class Eligibility(BaseModel):
    authorized_to_work_in: list[str] = Field(default_factory=list)
    requires_visa_sponsorship: bool = False
    willing_to_relocate: bool = False
    preferred_work_mode: str = ""


class Documents(BaseModel):
    resume_path: str = "data/resume.pdf"
    cover_letter_template_path: str = ""


class Profile(BaseModel):
    identity: Identity = Field(default_factory=Identity)
    links: Links = Field(default_factory=Links)
    employment: Employment = Field(default_factory=Employment)
    eligibility: Eligibility = Field(default_factory=Eligibility)
    documents: Documents = Field(default_factory=Documents)
    standard_answers: dict[str, str] = Field(default_factory=dict)

    def resume_file(self) -> Path:
        p = Path(self.documents.resume_path)
        return p if p.is_absolute() else ROOT / p

    def missing_required_fields(self) -> list[str]:
        """Fields the applier cannot function without. Checked before applying."""
        missing = []
        if not self.identity.first_name:
            missing.append("identity.first_name")
        if not self.identity.last_name:
            missing.append("identity.last_name")
        if not self.identity.email:
            missing.append("identity.email")
        if not self.identity.phone:
            missing.append("identity.phone")
        if not self.resume_file().exists():
            missing.append(f"documents.resume_path (no file at {self.resume_file()})")
        return missing


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open() as fh:
        return yaml.safe_load(fh) or {}


@lru_cache(maxsize=1)
def load_search_config() -> SearchConfig:
    return SearchConfig.model_validate(_read_yaml(CONFIG_DIR / "search.yaml"))


_SEARCH_HEADER = """\
# Where to look, what to keep, and how hard to push each site.
#
# Edited from the Search screen as well as by hand — this file is read on
# every run. PyYAML cannot round-trip comments, so the guidance here is
# regenerated on each save rather than preserved. Hand-written comments will
# not survive a save from the UI.

"""

# Re-emitted next to the site key on every write. Regenerating it rather than
# preserving it is the point: the warning cannot be lost, whether by editing
# the file or by saving over it from the browser.
_LINKEDIN_NOTE = """\
  # WARNING: LinkedIn's User Agreement prohibits automated access, and they
  # detect and restrict accounts that do it. There is no compliant way to
  # scrape or auto-apply here. Keep the cap low — every extra request is
  # risk — or set enabled: false.
"""


def save_search_config(cfg: SearchConfig) -> None:
    """Write search.yaml back to disk and drop the cached copy.

    The cache invalidation is the part that bites if forgotten:
    `load_search_config` is `lru_cache`d for the process lifetime, so a save
    without it leaves every later reader — including the running server —
    serving the values from before the edit.
    """
    body = yaml.safe_dump(
        cfg.model_dump(exclude_none=True), sort_keys=False, allow_unicode=True
    )
    if "\n  linkedin:\n" in body:
        body = body.replace("\n  linkedin:\n", "\n" + _LINKEDIN_NOTE + "  linkedin:\n", 1)
    (CONFIG_DIR / "search.yaml").write_text(_SEARCH_HEADER + body)
    load_search_config.cache_clear()


@lru_cache(maxsize=1)
def load_profile() -> Profile:
    path = CONFIG_DIR / "profile.yaml"
    if not path.exists():
        raise FileNotFoundError(
            f"No profile at {path}.\n"
            f"Copy {CONFIG_DIR / 'profile.example.yaml'} to profile.yaml and fill it in."
        )
    return Profile.model_validate(_read_yaml(path))


def env_value(name: str) -> str | None:
    """Read `name` from the environment, falling back to the project `.env`.

    Any provider's key resolves through here, so adding a provider does not
    mean adding another bespoke reader. A blank value counts as unset — the
    shipped `.env` template has `ANTHROPIC_API_KEY=` with nothing after it,
    and treating that as a key produces a 401 instead of "no key configured".
    """
    if value := os.environ.get(name):
        return value
    env_file = ROOT / ".env"
    if env_file.exists():
        prefix = f"{name}="
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line.startswith(prefix) and not line.startswith("#"):
                value = line.split("=", 1)[1].strip().strip("\"'")
                if value:
                    os.environ[name] = value
                    return value
    return None


def anthropic_api_key() -> str | None:
    """The Claude key specifically.

    Returns None rather than raising — the SDK also resolves credentials from
    an `ant auth login` profile, so an unset env var is not necessarily an error.
    """
    return env_value("ANTHROPIC_API_KEY")


def scoring_api_key(model: ModelConfig) -> str | None:
    """The key for whichever provider is configured, or None if none is needed."""
    name = model.key_env()
    return env_value(name) if name else None


def ensure_data_dirs() -> None:
    for sub in ("", "browser", "evidence"):
        (DATA_DIR / sub).mkdir(parents=True, exist_ok=True)
