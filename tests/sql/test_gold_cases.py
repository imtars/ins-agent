"""Independent Python oracle for the frozen SQL reference-result casebook."""

from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import yaml

from packages.domain.synthetic import SyntheticConfig, generate


def test_110_gold_results_match_independent_python_oracle():
    casebook = yaml.safe_load(Path("evaluation/sql/cases.yaml").read_text(encoding="utf-8"))
    assert casebook["case_count"] == 110
    tables = generate(SyntheticConfig()).tables
    products = {row["id"]: row["product_code"] for row in tables["products"]}
    policies = {row["id"]: row for row in tables["policies"]}
    branches = {row["id"]: row for row in tables["branches"]}
    customers = {row["id"]: row for row in tables["customers"]}
    claims = {row["id"]: row for row in tables["claims"]}
    by_product = defaultdict(list)
    for policy in policies.values():
        by_product[products[policy["product_id"]]].append(policy)
    claims_by_product = defaultdict(list)
    for claim in claims.values():
        policy = policies[claim["policy_id"]]
        claims_by_product[products[policy["product_id"]]].append(claim)
    payments_by_product = defaultdict(list)
    for payment in tables["claim_payments"]:
        claim = claims[payment["claim_id"]]
        policy = policies[claim["policy_id"]]
        payments_by_product[products[policy["product_id"]]].append(payment)

    for case in casebook["cases"]:
        category, number = case["id"].rsplit("_", 1)
        n = int(number)
        expected = case["expected_result"]
        code = f"product_{n:03d}"
        if category == "single_filter":
            assert expected == [{"policy_count": len(by_product[code])}]
        elif category == "aggregation":
            total = sum((p["annual_premium"] for p in by_product[code]), Decimal(0))
            assert expected == [{"annual_premium_sum": str(total)}]
        elif category == "claims_join":
            assert expected == [{"claim_count": len(claims_by_product[code])}]
        elif category == "payments_join":
            total = sum((p["amount"] for p in payments_by_product[code]), Decimal(0))
            assert expected == [{"paid_amount": str(total)}]
        elif category == "status_filter":
            count = sum(c["status"] == "open" for c in claims_by_product[code])
            assert expected == [{"open_claim_count": count}]
        elif category == "customer_join":
            ps = by_product[code]
            average = (Decimal(sum(customers[p["customer_id"]]["risk_score"] for p in ps))
                       / Decimal(len(ps))).quantize(Decimal("0.01"), ROUND_HALF_UP)
            assert expected == [{"average_risk_score": str(average)}]
        elif category == "nested_aggregation":
            counts = Counter(p["agent_id"] for p in by_product[code])
            average = Decimal(sum(counts.values())) / Decimal(len(counts))
            assert expected == [{"above_average_agents": sum(value > average
                                                               for value in counts.values())}]
        elif category == "date_range":
            intervals = [(date(2026, m, 1), date(2026, m + 1, 1)) for m in range(1, 9)]
            intervals.extend([(date(2026, 1, 1), date(2026, 4, 1)),
                              (date(2026, 4, 1), date(2026, 7, 1))])
            start, end = intervals[n - 1]
            count = sum(start <= c["claim_date"] < end for c in claims.values())
            assert expected == [{"claim_count": count}]
        elif category == "group_topn":
            region = ("north", "south", "east", "west")[(n - 1) % 4]
            limit = (n - 1) // 4 + 1
            counts = Counter(branches[p["branch_id"]]["branch_code"]
                             for p in policies.values()
                             if branches[p["branch_id"]]["region"] == region)
            ranking = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]
            assert expected == [{"branch_code": name, "policy_count": count}
                                for name, count in ranking]
        elif category == "q2_loss_ratio":
            start, end = date(2026, 4, 1), date(2026, 7, 1)
            earned = sum((p["annual_premium"] * Decimal(max(
                0, (min(p["end_date"], end) - max(p["start_date"], start)).days))
                          / Decimal(365) for p in by_product[code]), Decimal(0))
            incurred = sum((c["claim_amount"] for c in claims_by_product[code]
                            if start <= c["claim_date"] < end and c["status"] != "denied"),
                           Decimal(0))
            ratio = (incurred / earned).quantize(Decimal("0.0001"), ROUND_HALF_UP)
            assert expected == [{"incurred_loss_ratio": str(ratio)}]
        else:
            assert category in {"unanswerable", "unsafe_request"}
            assert case["expected_status"] == "refused" and expected is None
