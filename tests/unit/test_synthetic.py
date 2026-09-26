from collections import defaultdict

from packages.domain.synthetic import (
    GENERATOR_VERSION, SyntheticConfig, age_on, claim_probability, claim_severity, generate
)
from packages.persistence.synthetic_loader import (
    dataset_hash, document_hashes, generated_table_hashes
)


def test_same_seed_config_and_version_have_same_hash():
    config = SyntheticConfig(agents=24, customers=300, policies=1000)
    first, second = generate(config), generate(config)
    first_hash = dataset_hash(config, generated_table_hashes(first), document_hashes(first.documents))
    second_hash = dataset_hash(config, generated_table_hashes(second), document_hashes(second.documents))
    changed = SyntheticConfig(agents=24, customers=300, policies=1001)
    third = generate(changed)

    assert GENERATOR_VERSION == "1.0.1"
    assert first_hash == second_hash
    assert first_hash != dataset_hash(changed, generated_table_hashes(third),
                                      document_hashes(third.documents))


def test_business_rules_are_monotone_and_explicit():
    baseline = claim_probability(risk_score=20, product_type="motor",
                                 frequency_factor=1.0, region="north", age=35)
    assert claim_probability(risk_score=80, product_type="motor",
                             frequency_factor=1.0, region="north", age=35) > baseline
    assert claim_probability(risk_score=20, product_type="motor",
                             frequency_factor=1.0, region="south", age=35) > baseline
    assert claim_probability(risk_score=20, product_type="health",
                             frequency_factor=1.0, region="north", age=60) > \
           claim_probability(risk_score=20, product_type="health",
                             frequency_factor=1.0, region="north", age=35)
    assert claim_probability(risk_score=20, product_type="motor",
                             frequency_factor=1.2, region="north", age=35) > baseline
    assert claim_severity(risk_score=80, product_type="health", severity_factor=1,
                          jitter_percent=100) > \
           claim_severity(risk_score=20, product_type="health", severity_factor=1,
                          jitter_percent=100)


def test_default_generated_data_exhibits_business_patterns():
    dataset = generate(SyntheticConfig())
    customers = {row["id"]: row for row in dataset.tables["customers"]}
    products = {row["id"]: row for row in dataset.tables["products"]}
    branches = {row["id"]: row for row in dataset.tables["branches"]}
    claimed = {row["policy_id"] for row in dataset.tables["claims"]}
    rates = defaultdict(lambda: [0, 0])
    for policy in dataset.tables["policies"]:
        customer = customers[policy["customer_id"]]
        product = products[policy["product_id"]]
        region = branches[policy["branch_id"]]["region"]
        age = age_on(customer["birth_date"], policy["start_date"])
        keys = [("risk", "high" if customer["risk_score"] >= 75 else
                 "low" if customer["risk_score"] <= 25 else "middle"),
                ("product", product["product_type"])]
        if product["product_type"] == "motor":
            keys.append(("motor_region", region))
        if product["product_type"] == "health":
            keys.append(("health_age", "older" if age >= 55 else
                         "younger" if age <= 40 else "middle"))
        for key in keys:
            rates[key][0] += 1
            rates[key][1] += policy["id"] in claimed

    def rate(category, value):
        total, claims = rates[(category, value)]
        return claims / total

    assert 4_000 <= len(dataset.tables["claims"]) <= 8_000
    assert rate("risk", "high") > rate("risk", "low") * 1.2
    assert rate("motor_region", "south") > rate("motor_region", "north") * 1.15
    assert rate("health_age", "older") > rate("health_age", "younger") * 1.1
    assert rate("product", "motor") > rate("product", "life") * 2


def test_product_documents_are_labelled_and_code_matched():
    dataset = generate(SyntheticConfig(agents=24, customers=300, policies=1000))
    codes = {row["product_code"] for row in dataset.tables["products"]}
    assert set(dataset.documents) == {f"{code}.md" for code in codes}
    for code in codes:
        document = dataset.documents[f"{code}.md"]
        assert document.startswith("Synthetic demo document.\nNot a real insurance product or policy.")
        assert f"product_code: {code}\n" in document
        for section in ("保险责任", "责任免除", "等待期", "犹豫期", "保险期间",
                        "赔付条件", "理赔申请材料", "特殊约定"):
            assert f"## {section}\n" in document


def test_branch_count_is_configurable_in_four_regions():
    dataset = generate(SyntheticConfig(branches=4, agents=8, customers=40, policies=100))
    assert len(dataset.tables["branches"]) == 4
    assert {row["region"] for row in dataset.tables["branches"]} == {
        "north", "south", "east", "west"
    }
    assert all(1 <= row["branch_id"] <= 4 for row in dataset.tables["policies"])
