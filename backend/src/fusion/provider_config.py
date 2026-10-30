"""所选云模型供应商的配置加载。

这部分原本和真实 API 冒烟脚本写在同一个文件里，于是正式运行路径（编译任务与
其路由）不得不 import 一个名为 ``provider_smoke`` 的测试脚手架模块。抽出来之后，
生产代码只依赖本模块，冒烟脚本可以随测试脚手架一起排除出正式镜像。

这里只读取被选中那一家供应商的设置，不写入 ``os.environ``，也不会把其他供应商
的密钥带进来——同一局只冻结一家供应商，失败时不会自动改发给另一家。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from io import StringIO
from pathlib import Path
import re

from dotenv import dotenv_values

from src.fusion.budget import BudgetPolicy
from src.fusion.providers import PLAYER_PROVIDER_PROFILES, PlayerProviderProfile

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ENV_PATH = REPOSITORY_ROOT / ".env"
HARD_MAX_CONFIRMED_COST_CNY = Decimal("0.01")

_ENV_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
_SAFE_VERSION = re.compile(r"^[A-Za-z0-9_.:-]{1,100}$")


class SmokeConfigurationError(ValueError):
    """A safe, pre-network configuration refusal."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class SelectedSmokeConfig:
    profile: PlayerProviderProfile
    api_key: str
    base_url: str
    model: str
    pricing: BudgetPolicy

@dataclass(frozen=True)
class SelectedSmokeConfig:
    profile: PlayerProviderProfile
    api_key: str
    base_url: str
    model: str
    pricing: BudgetPolicy

def parse_confirmed_cost(raw: str) -> Decimal:
    try:
        amount = Decimal(raw)
    except (InvalidOperation, ValueError):
        raise SmokeConfigurationError("INVALID_CONFIRMED_COST") from None
    if not amount.is_finite() or amount <= 0 or amount > HARD_MAX_CONFIRMED_COST_CNY:
        raise SmokeConfigurationError("CONFIRMED_COST_OUT_OF_RANGE")
    return amount

def _selected_env_names(profile: PlayerProviderProfile) -> frozenset[str]:
    prefix = profile.pricing_env_prefix
    return frozenset({
        profile.api_key_env,
        profile.base_url_env,
        profile.model_env,
        f"{prefix}_INPUT_COST_PER_MILLION",
        f"{prefix}_CACHED_INPUT_COST_PER_MILLION",
        f"{prefix}_OUTPUT_COST_PER_MILLION",
        f"{prefix}_PRICING_VERSION",
    })

def read_selected_dotenv(env_path: Path, profile: PlayerProviderProfile) -> dict[str, str]:
    """Extract only the selected profile's settings without populating os.environ.

    Lines for every other provider are discarded before dotenv value parsing.
    Multiline values and duplicate selected keys are rejected to keep the
    credential path simple and auditable.
    """
    try:
        if not env_path.is_file() or env_path.stat().st_size > 1_000_000:
            raise SmokeConfigurationError("ENV_FILE_UNAVAILABLE")
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        raise SmokeConfigurationError("ENV_FILE_UNAVAILABLE") from None

    wanted = _selected_env_names(profile)
    selected: dict[str, str] = {}
    for line in lines:
        match = _ENV_ASSIGNMENT.match(line)
        if match is None or match.group(1) not in wanted:
            continue
        name = match.group(1)
        if name in selected:
            raise SmokeConfigurationError("DUPLICATE_SELECTED_SETTING")
        parsed = dotenv_values(stream=StringIO(line), interpolate=False)
        value = parsed.get(name)
        if not isinstance(value, str):
            raise SmokeConfigurationError("INVALID_SELECTED_SETTING")
        selected[name] = value.strip()
    return selected

def _positive_decimal(settings: dict[str, str], name: str) -> Decimal:
    try:
        value = Decimal(settings[name])
    except (KeyError, InvalidOperation, ValueError):
        raise SmokeConfigurationError("PRICING_CONFIGURATION_REQUIRED") from None
    if not value.is_finite() or value <= 0:
        raise SmokeConfigurationError("PRICING_CONFIGURATION_REQUIRED")
    return value

def load_selected_config(provider: str, env_path: Path = DEFAULT_ENV_PATH) -> SelectedSmokeConfig:
    profile = PLAYER_PROVIDER_PROFILES.get(provider)
    if profile is None:
        raise SmokeConfigurationError("UNSUPPORTED_PROVIDER")
    settings = read_selected_dotenv(env_path, profile)
    api_key = settings.get(profile.api_key_env, "")
    if not api_key or api_key.upper().startswith("CHANGE_ME"):
        raise SmokeConfigurationError("SELECTED_API_KEY_REQUIRED")

    base_url = settings.get(profile.base_url_env, profile.default_base_url).rstrip("/")
    model = settings.get(profile.model_env, profile.default_model)
    if base_url not in profile.allowed_base_urls:
        raise SmokeConfigurationError("BASE_URL_NOT_ALLOWLISTED")
    if model not in profile.allowed_models:
        raise SmokeConfigurationError("MODEL_NOT_ALLOWLISTED")

    prefix = profile.pricing_env_prefix
    pricing_version = settings.get(f"{prefix}_PRICING_VERSION", "")
    if (
        not pricing_version
        or pricing_version.upper().startswith("CHANGE_ME")
        or _SAFE_VERSION.fullmatch(pricing_version) is None
    ):
        raise SmokeConfigurationError("PRICING_CONFIGURATION_REQUIRED")
    pricing = BudgetPolicy(
        token_limit=10_000,
        cost_limit_cny=HARD_MAX_CONFIRMED_COST_CNY,
        input_rate_cny=_positive_decimal(settings, f"{prefix}_INPUT_COST_PER_MILLION"),
        cached_input_rate_cny=_positive_decimal(settings, f"{prefix}_CACHED_INPUT_COST_PER_MILLION"),
        output_rate_cny=_positive_decimal(settings, f"{prefix}_OUTPUT_COST_PER_MILLION"),
        paid_calls_enabled=True,
        pricing_version=pricing_version[:100],
    )
    return SelectedSmokeConfig(profile, api_key, base_url, model, pricing)
