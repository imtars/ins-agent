"""Deterministic, wholly fictional insurance operations and product documents."""

from dataclasses import asdict, dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
import random

SEED = 202609
GENERATOR_VERSION = "1.1.0"
TABLE_ORDER = (
    "branches", "agents", "customers", "products", "policies", "claims", "claim_payments"
)
REGIONS = ("north", "south", "east", "west")
BASE_PREMIUM = {"motor": 3600, "health": 3000, "accident": 1700, "life": 4400}
BASE_SEVERITY = {"motor": 6800, "health": 9500, "accident": 4200, "life": 60000}
BASE_FREQUENCY = {"motor": 0.32, "health": 0.29, "accident": 0.23, "life": 0.09}
MONEY = Decimal("0.01")
OBSERVATION_START = date(2026, 1, 1)
OBSERVATION_END = date(2026, 8, 31)


@dataclass(frozen=True)
class ProductProfile:
    code: str
    name: str
    product_type: str
    frequency_factor: Decimal
    severity_factor: Decimal
    covered_events: str
    exclusions: str
    waiting_days: int
    deductible_yuan: int
    copay_percent: int
    annual_limit_yuan: int


# These are simulator parameters, not columns in the operational database.
# Broader illustrative cover corresponds to more eligible events and larger
# simulated claim amounts; the values are not fitted actuarial estimates.
PRODUCT_PROFILES = (
    ProductProfile("product_001", "机动车基础版", "motor", Decimal("0.850"), Decimal("0.900"),
                   "车辆意外碰撞造成的车身损失。",
                   "盗抢、暴雨积水、故意损坏及酒后驾驶造成的损失。", 0, 3000, 0, 60000),
    ProductProfile("product_002", "机动车优选版", "motor", Decimal("1.025"), Decimal("1.050"),
                   "车辆意外碰撞或整车盗抢造成的直接损失。",
                   "暴雨积水、故意损坏及酒后驾驶造成的损失。", 0, 1500, 0, 150000),
    ProductProfile("product_003", "机动车尊享版", "motor", Decimal("1.200"), Decimal("1.200"),
                   "车辆意外碰撞、整车盗抢或暴雨积水造成的直接损失。",
                   "故意损坏及酒后驾驶造成的损失。", 0, 500, 0, 300000),
    ProductProfile("product_004", "健康基础版", "health", Decimal("0.850"), Decimal("0.900"),
                   "疾病住院产生的合规医疗费用。",
                   "门诊、牙科、美容用途费用和未如实告知的既往疾病费用。", 60, 0, 20, 100000),
    ProductProfile("product_005", "健康优选版", "health", Decimal("1.025"), Decimal("1.050"),
                   "疾病住院及急诊门诊产生的合规医疗费用。",
                   "普通门诊、牙科、美容用途费用和未如实告知的既往疾病费用。", 30, 0, 10, 300000),
    ProductProfile("product_006", "健康尊享版", "health", Decimal("1.200"), Decimal("1.200"),
                   "疾病住院、急诊门诊及普通门诊产生的合规医疗费用。",
                   "牙科、美容用途费用和未如实告知的既往疾病费用。", 15, 0, 0, 1000000),
    ProductProfile("product_007", "意外基础版", "accident", Decimal("0.850"), Decimal("0.900"),
                   "外来突发意外造成的直接伤害治疗费用。",
                   "救护车、康复治疗、故意自伤及疾病本身导致的费用。", 0, 500, 0, 50000),
    ProductProfile("product_008", "意外优选版", "accident", Decimal("1.025"), Decimal("1.050"),
                   "外来突发意外造成的直接伤害治疗及救护车费用。",
                   "康复治疗、高风险竞技运动、故意自伤及疾病本身导致的费用。", 0, 200, 0, 150000),
    ProductProfile("product_009", "意外尊享版", "accident", Decimal("1.200"), Decimal("1.200"),
                   "外来突发意外造成的直接伤害治疗、救护车及康复治疗费用。",
                   "故意自伤及疾病本身导致的费用。", 0, 0, 0, 300000),
    ProductProfile("product_010", "寿险基础版", "life", Decimal("0.850"), Decimal("0.900"),
                   "保险期间内的身故给付。",
                   "全残、特定重大疾病及投保人故意造成的事故。", 0, 0, 0, 100000),
    ProductProfile("product_011", "寿险优选版", "life", Decimal("1.025"), Decimal("1.050"),
                   "保险期间内的身故或全残给付。",
                   "特定重大疾病单独给付及投保人故意造成的事故。", 0, 0, 0, 300000),
    ProductProfile("product_012", "寿险尊享版", "life", Decimal("1.200"), Decimal("1.200"),
                   "保险期间内的身故、全残或特定重大疾病给付。",
                   "投保人故意造成的事故。", 0, 0, 0, 600000),
)
PROFILES_BY_CODE = {profile.code: profile for profile in PRODUCT_PROFILES}


@dataclass(frozen=True)
class SyntheticConfig:
    seed: int = SEED
    branches: int = 12
    agents: int = 180
    customers: int = 10_000
    products: int = 12
    policies: int = 30_000

    def __post_init__(self):
        if self.branches < 4 or self.branches % 4 or self.products != 12:
            raise ValueError("branches must be a positive multiple of four; M1 requires 12 products")
        if self.agents < self.branches or self.customers < 1 or self.policies < 1:
            raise ValueError("agents, customers and policies must be positive")
        if self.seed != SEED:
            raise ValueError(f"M1 seed is fixed to {SEED}")

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SyntheticDataset:
    tables: dict[str, list[dict]]
    documents: dict[str, str]


def age_on(born: date, day: date) -> int:
    return day.year - born.year - ((day.month, day.day) < (born.month, born.day))


def claim_probability(*, risk_score: int, product_type: str, frequency_factor: float,
                      region: str, age: int) -> float:
    """Annual probability for a full year of eligible exposure."""
    risk_multiplier = 0.70 + risk_score / 150
    age_multiplier = 1.25 if age >= 55 and product_type in ("health", "life") else 1.0
    region_multiplier = 1.40 if region == "south" and product_type == "motor" else 1.0
    return min(0.80, BASE_FREQUENCY[product_type] * frequency_factor
               * risk_multiplier * age_multiplier * region_multiplier)


def exposure_adjusted_probability(annual_probability: float, exposure_days: int) -> float:
    """Scale a full-year Bernoulli risk to the eligible observation interval."""
    if not 0 <= annual_probability <= 1 or not 0 <= exposure_days <= 365:
        raise ValueError("annual probability and exposure days out of range")
    return 1 - (1 - annual_probability) ** (exposure_days / 365)


def claim_severity(*, risk_score: int, product_type: str,
                   severity_factor: Decimal, jitter_percent: int) -> Decimal:
    base = Decimal(BASE_SEVERITY[product_type])
    risk_multiplier = Decimal(1) + Decimal(risk_score) / Decimal(500)
    return (base * severity_factor * risk_multiplier
            * Decimal(jitter_percent) / Decimal(100)).quantize(MONEY, ROUND_HALF_UP)


def product_documents(products: list[dict]) -> dict[str, str]:
    documents = {}
    for product in products:
        code = product["product_code"]
        kind = product["product_type"]
        profile = PROFILES_BY_CODE[code]
        if kind != profile.product_type:
            raise ValueError(f"product type mismatch for {code}")
        waiting_text = (
            f"疾病责任等待期为 {profile.waiting_days} 天，自保单起始日起计算；"
            "等待期内的疾病事故不计入可赔责任。"
            if profile.waiting_days else "本演示产品不设等待期。"
        )
        text = (
            "Synthetic demo document.\n"
            "Not a real insurance product or policy.\n\n"
            f"# {product['name']}\n\n"
            f"product_code: {code}\n"
            f"product_type: {kind}\n\n"
            "## 保险责任\n"
            f"在保险期间内，保障以下演示事件：{profile.covered_events}\n\n"
            "## 责任免除\n"
            f"以下情形不在本演示保障内：{profile.exclusions}\n\n"
            "## 等待期\n"
            f"{waiting_text}\n\n"
            "## 犹豫期\n"
            "本演示产品的犹豫期为 15 天，自签收演示保单之日起计算。\n\n"
            "## 保险期间\n"
            "保险期间为保单起始日（含）至终止日（不含），具体日期以合成保单记录为准。\n\n"
            "## 赔付条件\n"
            f"事故须符合保险责任且不属于责任免除。每次免赔额为 {profile.deductible_yuan} 元，"
            f"免赔后自付比例为 {profile.copay_percent}%，演示年度赔付限额为 "
            f"{profile.annual_limit_yuan} 元。\n\n"
            "## 理赔申请材料\n"
            "提交演示保单编号、事故说明和相关费用或事故证明材料。\n\n"
            "## 特殊约定\n"
            "本文件仅供软件演示，与任何真实保险公司的产品、费率和合同无关。"
            "运营库理赔金额是按生成器规则产生的核定演示金额，不表示逐项执行本条款的理算结果。\n"
        )
        documents[f"{code}.md"] = text
    return documents


def generate(config: SyntheticConfig) -> SyntheticDataset:
    rng = random.Random(config.seed)
    tables: dict[str, list[dict]] = {name: [] for name in TABLE_ORDER}
    branch_agents: dict[int, list[int]] = {i: [] for i in range(1, config.branches + 1)}

    for i in range(1, config.branches + 1):
        region = REGIONS[(i - 1) // (config.branches // 4)]
        tables["branches"].append({"id": i, "branch_code": f"BR{i:03d}",
                                   "name": f"Synthetic {region.title()} Branch {i:02d}",
                                   "region": region})

    for i in range(1, config.agents + 1):
        branch_id = (i - 1) % config.branches + 1
        branch_agents[branch_id].append(i)
        tables["agents"].append({"id": i, "agent_code": f"AG{i:05d}",
                                 "branch_id": branch_id,
                                 "joined_on": date(2021, 1, 1) + timedelta(days=rng.randrange(1500))})

    birth_origin = date(1955, 1, 1)
    for i in range(1, config.customers + 1):
        tables["customers"].append({"id": i, "customer_code": f"CU{i:06d}",
                                    "home_branch_id": rng.randint(1, config.branches),
                                    "birth_date": birth_origin + timedelta(days=rng.randrange(52 * 365)),
                                    "risk_score": rng.randint(0, 100)})

    for i, profile in enumerate(PRODUCT_PROFILES, start=1):
        kind = profile.product_type
        variant = (i - 1) % 3
        tables["products"].append({
            "id": i, "product_code": profile.code,
            "name": f"Synthetic {profile.name}", "product_type": kind,
            "annual_base_premium": Decimal(BASE_PREMIUM[kind]) * (Decimal(1) + Decimal(variant) / 10),
        })

    policy_origin = date(2025, 7, 1)
    payment_id = 0
    for i in range(1, config.policies + 1):
        customer = tables["customers"][rng.randrange(config.customers)]
        product = tables["products"][rng.randrange(12)]
        branch_id = customer["home_branch_id"]
        agent_id = rng.choice(branch_agents[branch_id])
        start = policy_origin + timedelta(days=rng.randrange(365))
        end = start + timedelta(days=365)
        premium = (product["annual_base_premium"]
                   * (Decimal(1) + Decimal(customer["risk_score"]) / Decimal(500))
                   * Decimal(rng.randint(90, 110)) / Decimal(100)).quantize(MONEY, ROUND_HALF_UP)
        tables["policies"].append({
            "id": i, "policy_code": f"PO{i:07d}", "customer_id": customer["id"],
            "agent_id": agent_id, "branch_id": branch_id, "product_id": product["id"],
            "start_date": start, "end_date": end, "annual_premium": premium,
        })
        profile = PROFILES_BY_CODE[product["product_code"]]
        earliest = max(start + timedelta(days=profile.waiting_days), OBSERVATION_START)
        latest = min(end - timedelta(days=1), OBSERVATION_END)
        if earliest > latest:
            continue
        annual_probability = claim_probability(
            risk_score=customer["risk_score"], product_type=product["product_type"],
            frequency_factor=float(profile.frequency_factor),
            region=tables["branches"][branch_id - 1]["region"],
            age=age_on(customer["birth_date"], start),
        )
        exposure_days = (latest - earliest).days + 1
        probability = exposure_adjusted_probability(annual_probability, exposure_days)
        if rng.random() >= probability:
            continue
        claim_date = earliest + timedelta(days=rng.randrange((latest - earliest).days + 1))
        amount = claim_severity(
            risk_score=customer["risk_score"], product_type=product["product_type"],
            severity_factor=profile.severity_factor, jitter_percent=rng.randint(75, 135)
        )
        draw = rng.random()
        status = "settled" if draw < 0.78 else "open" if draw < 0.96 else "denied"
        claim_id = len(tables["claims"]) + 1
        tables["claims"].append({"id": claim_id, "claim_code": f"CL{claim_id:07d}",
                                 "policy_id": i, "claim_date": claim_date,
                                 "claim_amount": amount, "status": status})
        if status == "settled":
            first = amount if rng.random() < 0.7 else (amount / 2).quantize(MONEY, ROUND_HALF_UP)
            for part, paid in enumerate((first, amount - first)):
                if paid <= 0:
                    continue
                payment_id += 1
                tables["claim_payments"].append({
                    "id": payment_id, "claim_id": claim_id,
                    "payment_date": claim_date + timedelta(days=14 + 7 * part), "amount": paid,
                })

    return SyntheticDataset(tables=tables, documents=product_documents(tables["products"]))
