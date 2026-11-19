"""Load the valuation inputs (country file, market file, sector map), each with its source.

Nothing here is hardcoded: every number comes from `config/`. A value is usable only when
`verified = true`, unless the caller overrides that on purpose; the result then lists every
unverified input next to the number.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG = Path(__file__).resolve().parents[2] / "config"


@dataclass(frozen=True)
class Input:
    name: str
    value: object
    source: str
    as_of: str
    verified: bool
    note: str = ""

    def public(self) -> dict:
        return {"name": self.name, "value": self.value, "source": self.source, "as_of": self.as_of,
                "verified": self.verified, "note": self.note}


@dataclass
class Assumptions:
    country: str
    inputs: dict[str, Input]
    betas: dict[str, dict]
    sectors: dict[str, dict]
    liquidity: dict
    forecast: dict
    sensitivity: dict
    sector_models: dict = field(default_factory=dict)
    used: set[str] = field(default_factory=set)

    def get(self, name: str):
        self.used.add(name)
        return self.inputs[name].value

    def unverified_used(self) -> list[Input]:
        return [self.inputs[n] for n in sorted(self.used) if not self.inputs[n].verified]


def _load(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def _input(name: str, raw: dict, value=None) -> Input:
    return Input(name, raw.get("value") if value is None else value, raw.get("source", ""),
                 str(raw.get("as_of", "")), bool(raw.get("verified", False)), raw.get("note", ""))


def load(country: str = "KH", config_dir: Path = CONFIG) -> Assumptions:
    c = _load(config_dir / "countries" / f"{country}.toml")
    m = _load(config_dir / "market.toml")
    s = _load(config_dir / "sectors.toml")
    inputs = {
        "risk_free_rate": _input("risk_free_rate", m["risk_free_rate"]),
        "mature_equity_risk_premium": _input("mature_equity_risk_premium", m["mature_equity_risk_premium"]),
        "terminal_growth": _input("terminal_growth", m["terminal_growth"]),
        "country_risk_premium": _input("country_risk_premium", c["country_risk_premium"]),
        "default_spread": _input("default_spread", c["default_spread"]),
        "corporate_tax_rate": _input("corporate_tax_rate", c["corporate_tax_rate"]),
        "liquidity_discount": _input("liquidity_discount", c["liquidity_discount"], value="bands"),
        "industry_betas": _input("industry_betas", m["betas"], value="table"),
        "forecast": _input("forecast", m["forecast"], value="settings"),
        "sensitivity": _input("sensitivity", m["sensitivity"], value="settings"),
        "sector_map": Input("sector_map", "table", s.get("source", ""), "", bool(s.get("verified", False))),
    }
    return Assumptions(country=country, inputs=inputs, betas=m["betas"]["industries"],
                       sectors=s.get("companies", {}), liquidity=c["liquidity_discount"],
                       forecast=m["forecast"], sensitivity=m["sensitivity"],
                       sector_models=s.get("sector_models", {}))
