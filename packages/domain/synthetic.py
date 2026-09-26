"""Deterministic, wholly fictional insurance operations and product documents."""

from dataclasses import asdict, dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
import random

SEED = 202609
GENERATOR_VERSION = "1.0.1"
TABLE_ORDER = (
    "branches", "agents", "customers", "products", "policies", "claims", "claim_payments"
)
REGIONS = ("north", "south", "east", "west")
PRODUCT_TYPES = ("motor", "health", "accident", "life")
BASE_PREMIUM = {"motor": 3600, "health": 3000, "accident": 1700, "life": 4400}
BASE_SEVERITY = {"motor": 6800, "health": 9500, "accident": 4200, "life": 60000}
BASE_FREQUENCY = {"motor": 0.22, "health": 0.19, "accident": 0.15, "life": 0.06}
MONEY = Decimal("0.01")


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
    """Documented monotone business rules, not a fitted actuarial model."""
    risk_multiplier = 0.70 + risk_score / 150
    age_multiplier = 1.25 if age >= 55 and product_type in ("health", "life") else 1.0
    region_multiplier = 1.40 if region == "south" and product_type == "motor" else 1.0
    return min(0.80, BASE_FREQUENCY[product_type] * frequency_factor
               * risk_multiplier * age_multiplier * region_multiplier)


def claim_severity(*, risk_score: int, product_type: str,
                   severity_factor: Decimal, jitter_percent: int) -> Decimal:
    base = Decimal(BASE_SEVERITY[product_type])
    risk_multiplier = Decimal(1) + Decimal(risk_score) / Decimal(500)
    return (base * severity_factor * risk_multiplier
            * Decimal(jitter_percent) / Decimal(100)).quantize(MONEY, ROUND_HALF_UP)


def product_documents(products: list[dict]) -> dict[str, str]:
    responsibility = {
        "motor": "在保险期间内，约定机动车发生意外碰撞并产生经核定的车辆损失时，按本演示条款赔付。",
        "health": "在保险期间内，被保险人因疾病住院产生符合约定的医疗费用时，按本演示条款赔付。",
        "accident": "在保险期间内，被保险人遭受外来突发意外伤害时，按本演示条款赔付。",
        "life": "在保险期间内发生约定身故事件时，按本演示条款赔付。",
    }
    exclusions = {
        "motor": "故意损坏、酒后驾驶及未获许可的竞赛活动造成的损失不在本演示保障内。",
        "health": "投保前已明确存在且未如实告知的疾病，以及美容用途费用不在本演示保障内。",
        "accident": "自伤、故意犯罪及疾病本身导致的损害不在本演示保障内。",
        "life": "投保人故意造成的保险事故不在本演示保障内。",
    }
    documents = {}
    for product in products:
        code = product["product_code"]
        kind = product["product_type"]
        waiting_days = 30 if kind == "health" else 0
        text = (
            "Synthetic demo document.\n"
            "Not a real insurance product or policy.\n\n"
            f"# {product['name']}\n\n"
            f"product_code: {code}\n"
            f"product_type: {kind}\n\n"
            "## 保险责任\n"
            f"{responsibility[kind]}\n\n"
            "## 责任免除\n"
            f"{exclusions[kind]}\n\n"
            "## 等待期\n"
            f"等待期为 {waiting_days} 天；等待期内发生的疾病事故不予赔付。意外事故不设等待期。\n\n"
            "## 犹豫期\n"
            "本演示产品的犹豫期为 15 天，自签收演示保单之日起计算。\n\n"
            "## 保险期间\n"
            "保险期间为保单起始日（含）至终止日（不含），具体日期以合成保单记录为准。\n\n"
            "## 赔付条件\n"
            "事故发生于保险期间内、符合保险责任且不属于责任免除时，按核定金额赔付。\n\n"
            "## 理赔申请材料\n"
            "提交演示保单编号、事故说明和相关费用或事故证明材料。\n\n"
            "## 特殊约定\n"
            "本文件仅供软件演示，与任何真实保险公司的产品、费率和合同无关。\n"
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

    product_names = ("机动车基础", "机动车优选", "机动车高风险", "健康基础", "健康优选", "健康高保障",
                     "意外基础", "意外优选", "意外高保障", "寿险基础", "寿险优选", "寿险高保障")
    for i in range(1, 13):
        kind = PRODUCT_TYPES[(i - 1) // 3]
        variant = (i - 1) % 3
        tables["products"].append({
            "id": i, "product_code": f"product_{i:03d}",
            "name": f"Synthetic {product_names[i - 1]}", "product_type": kind,
            "annual_base_premium": Decimal(BASE_PREMIUM[kind]) * (Decimal(1) + Decimal(variant) / 10),
            "claim_frequency_factor": Decimal("0.850") + Decimal(variant) * Decimal("0.175"),
            "claim_severity_factor": Decimal("0.900") + Decimal(variant) * Decimal("0.150"),
        })

    policy_origin = date(2025, 7, 1)
    claim_window_start = date(2026, 1, 1)
    claim_window_end = date(2026, 8, 31)
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
        probability = claim_probability(
            risk_score=customer["risk_score"], product_type=product["product_type"],
            frequency_factor=float(product["claim_frequency_factor"]),
            region=tables["branches"][branch_id - 1]["region"],
            age=age_on(customer["birth_date"], start),
        )
        if rng.random() >= probability:
            continue
        earliest = max(start, claim_window_start)
        latest = min(end - timedelta(days=1), claim_window_end)
        if earliest > latest:
            continue
        claim_date = earliest + timedelta(days=rng.randrange((latest - earliest).days + 1))
        amount = claim_severity(
            risk_score=customer["risk_score"], product_type=product["product_type"],
            severity_factor=product["claim_severity_factor"], jitter_percent=rng.randint(75, 135)
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
