"""对已保存的旅行计划执行可重复、无外部 API 依赖的离线评测。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import mean
from typing import Any

from app.models.schemas import TripPlan, TripRequest
from app.services.trip_quality_service import trip_quality_service


def evaluate_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    metrics: list[dict[str, Any]] = []
    invalid_cases = 0

    for record in records:
        try:
            request = TripRequest.model_validate(record["request"], context={"from_storage": True})
            plan = TripPlan.model_validate(record["plan"])
        except Exception:
            invalid_cases += 1
            continue

        original_budget = plan.budget.total if plan.budget else None
        finalized = trip_quality_service.finalize(
            plan,
            request,
            intercity_cost_per_person=int(record.get("intercity_cost_per_person", 0)),
            bind_meals=False,  # 离线评测不调用实时 MCP 餐厅绑定，保证可重复
        )
        day_count_match = len(finalized.days) == request.travel_days
        executable_days = sum(
            1
            for day in finalized.days
            if day.attractions
            and {"breakfast", "lunch", "dinner"}.issubset({meal.type for meal in day.meals})
        )
        metrics.append({
            "day_count_match": day_count_match,
            "executable_day_rate": executable_days / request.travel_days,
            "warning_count": len(finalized.warnings),
            "route_saved_percent": finalized.optimization_summary.saved_percent,
            "budget_drift": (
                original_budget is not None and original_budget != finalized.budget.total
            ),
            "over_budget": finalized.budget.over_budget,
        })

    total = len(records)
    valid = len(metrics)
    return {
        "total_cases": total,
        "valid_cases": valid,
        "schema_valid_rate": round(valid / total, 4) if total else 0,
        "day_count_match_rate": round(mean(item["day_count_match"] for item in metrics), 4) if metrics else 0,
        "executable_day_rate": round(mean(item["executable_day_rate"] for item in metrics), 4) if metrics else 0,
        "average_warning_count": round(mean(item["warning_count"] for item in metrics), 2) if metrics else 0,
        "average_route_saved_percent": round(mean(item["route_saved_percent"] for item in metrics), 2) if metrics else 0,
        "budget_drift_rate": round(mean(item["budget_drift"] for item in metrics), 4) if metrics else 0,
        "over_budget_rate": round(mean(item["over_budget"] for item in metrics), 4) if metrics else 0,
        "invalid_cases": invalid_cases,
    }


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    records = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number} 不是合法JSON") from exc
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description="旅行Agent离线评测")
    parser.add_argument("dataset", type=Path, help="JSONL评测集路径")
    parser.add_argument("--output", type=Path, help="可选的指标JSON输出路径")
    args = parser.parse_args()

    result = evaluate_records(load_jsonl(args.dataset))
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

