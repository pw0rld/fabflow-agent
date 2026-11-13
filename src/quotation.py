#!/usr/bin/env python3
"""Deterministic cost calculation and immutable quotation drafts."""
from __future__ import annotations

import argparse
import copy
from decimal import Decimal, DecimalException, ROUND_HALF_UP, localcontext
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

from prep_record import RecordError, RecordStore, identifier, number, only_keys, require, stamp

VERSION = "0.2.0"
CENT = Decimal("0.01")
POLICY_KEYS = {"schema_version", "pricing_method", "rate", "currency", "tax_included",
               "freight_included", "simulated", "actor", "source_text"}


def sha(content):
    return hashlib.sha256(content).hexdigest()


def money(value):
    return format(value.quantize(CENT, rounding=ROUND_HALF_UP), ".2f")


def json_text(value):
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def validate_policy(policy):
    only_keys(policy, POLICY_KEYS, "pricing policy")
    require(POLICY_KEYS == set(policy), "all pricing policy fields must be explicit")
    require(type(policy["schema_version"]) is int and policy["schema_version"] == 1, "unsupported policy schema")
    require(policy["pricing_method"] in {"cost_plus", "gross_margin"}, "unsupported pricing method")
    require(isinstance(policy["rate"], str) and bool(policy["rate"].strip()), "pricing rate must be a decimal string")
    rate = number(policy["rate"])
    require(rate >= 0, "negative pricing rate")
    if policy["pricing_method"] == "gross_margin":
        require(rate < 1, "gross margin must be less than 1")
    require(policy["currency"] == "CNY", "only CNY is supported")
    require(policy["tax_included"] is False and policy["freight_included"] is False,
            "this version requires explicit tax excluded and freight excluded")
    require(type(policy["simulated"]) is bool, "explicit policy simulated boolean required")
    for key in ("actor", "source_text"):
        require(isinstance(policy[key], str) and bool(policy[key].strip()), key + " required")
    return rate


class QuoteStore:
    def __init__(self, root):
        self.store = RecordStore(root)

    def _calculate(self, record, policy):
        rate = validate_policy(policy)
        validation = self.store._validation(record)
        require(validation["ready"], "inquiry inputs are not ready: " + validation["status"])
        require(type(record["simulated"]) is bool, "record simulated flag required")

        def field(name, unit=None):
            slot = record["fields"][name]
            require(slot["status"] == "verified" and isinstance(slot["effective"], dict), name + " not verified")
            result = slot["effective"]
            if unit is not None:
                require(result["unit"] == unit, name + " has unsupported unit")
            return result["value"]

        def nonnegative(value, label):
            result = number(value)
            require(result >= 0, label + " must not be negative")
            return result

        quantity = field("quantity", "piece")
        require(type(quantity) is int and quantity > 0, "quantity must be a positive integer")
        usage = nonnegative(field("material_usage_per_piece", "kg/piece"), "material usage")
        require(usage > 0, "material usage must be positive")
        material_rate = nonnegative(field("material_rate", "CNY/kg"), "material rate")
        setup = nonnegative(field("setup_fee", "CNY/order"), "setup fee")
        route = field("process_route")
        rates = field("operation_rates")
        require(isinstance(route, list) and route and all(isinstance(x, str) and x.strip() for x in route), "process route required")
        require(len({x.casefold().strip() for x in route}) == len(route), "duplicate process route")
        require(isinstance(rates, list) and rates, "operation rates required")
        names = []
        for row in rates:
            only_keys(row, {"operation", "value", "unit"}, "operation rate")
            require(set(row) == {"operation", "value", "unit"}, "incomplete operation rate")
            require(isinstance(row["operation"], str) and row["operation"].strip(), "operation name required")
            names.append(row["operation"])
        require(len({x.casefold().strip() for x in names}) == len(names), "duplicate operation rate")
        require(set(names) == set(route), "each process route operation needs exactly one matching rate")
        inclusions = field("fee_inclusions")
        require(isinstance(inclusions, list) and inclusions and all(isinstance(x, str) and x.strip() for x in inclusions), "fee inclusions required")
        engineering = record["fields"].get("engineering_open_items", {}).get("effective")
        limitations = copy.deepcopy(engineering["value"] if engineering else [])
        require(isinstance(limitations, list) and all(isinstance(x, str) for x in limitations), "invalid engineering limitations")
        limitations += [x for x in record.get("notes", []) if "替代关系" in x and "未验证" in x and x not in limitations]

        with localcontext() as context:
            context.prec = 64
            material = usage * material_rate * quantity
            lines = [{"kind": "material", "operation": "材料", "rate": str(material_rate),
                      "rate_unit": "CNY/kg", "pricing_quantity": str(usage * quantity),
                      "quantity_unit": "kg", "raw_amount": str(material), "amount": money(material),
                      "record_fields": ["material_usage_per_piece", "material_rate", "quantity"]}]
            process = Decimal(0)
            ordered = {row["operation"]: row for row in rates}
            for operation in route:
                row = ordered[operation]
                unit = row["unit"]
                require(unit in {"CNY/piece", "CNY/order"},
                        f"{operation}: {unit} requires an explicit measured pricing quantity; cannot calculate")
                unit_rate = nonnegative(row["value"], operation + " rate")
                require(not (unit == "CNY/order" and unit_rate > 0 and setup > 0),
                        "positive per-order process fee and separate setup fee need inclusion review; "
                        "this version does not add both; clarify the technical inputs first")
                count = quantity if unit == "CNY/piece" else 1
                amount = unit_rate * count
                process += amount
                lines.append({"kind": "process", "operation": operation, "rate": str(unit_rate),
                              "rate_unit": unit, "pricing_quantity": str(count),
                              "quantity_unit": "piece" if unit == "CNY/piece" else "order",
                              "raw_amount": str(amount), "amount": money(amount),
                              "record_fields": ["process_route", "operation_rates", "quantity"]})
            lines.append({"kind": "setup", "operation": "准备费用", "rate": str(setup), "rate_unit": "CNY/order",
                          "pricing_quantity": "1", "quantity_unit": "order", "raw_amount": str(setup),
                          "amount": money(setup), "record_fields": ["setup_fee"]})
            cost = material + process + setup
            target = cost * (1 + rate) if policy["pricing_method"] == "cost_plus" else cost / (1 - rate)
            unit_price = (target / quantity).quantize(CENT, rounding=ROUND_HALF_UP)
            total_price = unit_price * quantity
            markup = Decimal(money(target - cost))
            total_cost = Decimal(money(cost))
            adjustment = total_price - total_cost - markup
            amounts = {"material_cost": money(material), "process_cost": money(process),
                       "setup_cost": money(setup), "total_cost": money(cost), "unit_cost": money(cost / quantity),
                       "unit_price": money(unit_price), "total_price": money(total_price),
                       "markup_amount": money(markup), "rounding_adjustment": money(adjustment)}
        simulated = record["simulated"] or policy["simulated"]
        return {"schema_version": 1, "calculator_version": VERSION, "task_id": record["task_id"],
                "source_revision": record["data_revision"], "quantity": quantity, "currency": "CNY",
                "product": {key: field(key) for key in ("product_name", "material_family", "material_grade", "thickness", "surface_treatment")},
                "simulated": simulated, "status": "模拟报价草稿，待审核" if simulated else "报价草稿，待审核",
                "approved": False, "pricing_method": policy["pricing_method"], "pricing_rate": str(rate),
                "tax_included": False, "freight_included": False,
                "amounts": amounts, "cost_lines": lines, "raw_total_cost": str(cost),
                "raw_target_price": str(target), "fee_inclusions": copy.deepcopy(inclusions),
                "engineering_limitations": limitations,
                "rounding_rule": "Decimal; ROUND_HALF_UP; unit price to CNY 0.01; total = rounded unit price × quantity; cost sums before rounding"}

    def _quote_path(self, task_id, quote_id):
        task = self.store.task_path(task_id)
        parent = task / "quotes"
        require(not parent.is_symlink(), "symlink quotation directory prohibited")
        path = parent / identifier(quote_id)
        require(not path.is_symlink(), "symlink quote prohibited")
        return path

    def _load(self, task_id, quote_id):
        path = self._quote_path(task_id, quote_id)
        def read(name):
            p = path / name
            require(p.is_file() and not p.is_symlink(), "quotation snapshot missing: " + name)
            return p.read_bytes()
        result = json.loads(read("quote.json"))
        record_bytes, policy_bytes = read("record_snapshot.json"), read("pricing_policy.json")
        require(result["quote_id"] == quote_id and result["task_id"] == task_id, "quotation identity mismatch")
        require(result["source_record_sha256"] == sha(record_bytes), "quotation record snapshot changed")
        require(result["pricing_policy_sha256"] == sha(policy_bytes), "quotation policy snapshot changed")
        calculated = self._calculate(json.loads(record_bytes), json.loads(policy_bytes))
        require(all(result.get(k) == value for k, value in calculated.items()), "quotation amounts or calculation changed")
        current = self.store.task_path(task_id) / "record.json"
        result["source_current"] = current.is_file() and not current.is_symlink() and sha(current.read_bytes()) == result["source_record_sha256"]
        return result

    def load(self, task_id, quote_id):
        with self.store.lock(task_id):
            return self._load(task_id, quote_id)

    def paths(self, task_id, quote_id):
        path = self._quote_path(task_id, quote_id)
        return {"quote_json": str(path / "quote.json"), "quote_markdown": str(path / "quote.md"),
                "cost_markdown": str(path / "cost_breakdown.md")}

    def _documents(self, result):
        a, p = result["amounts"], result["product"]
        title = "模拟报价单（草稿）" if result["simulated"] else "报价单（草稿）"
        lines = ["# " + title, "", "报价编号：" + result["quote_id"], "", "状态：" + result["status"], ""]
        if not result.get("source_current", True):
            lines += ["**历史报价：当前任务资料已变化，本稿对应原有资料版本，需重新核算。**", ""]
        if result["simulated"]:
            lines += ["本稿使用模拟技术数据和演示计价策略，供流程测试；未取得工厂正式审核。", ""]
        lines += ["| 产品 | 材质 | 厚度 | 表面处理 | 数量 | 未税单价 | 未税金额 |",
                  "|---|---|---|---|---:|---:|---:|",
                  "| " + " | ".join(cell(x) for x in (p["product_name"], p["material_grade"],
                      str(p["thickness"]) + " mm", p["surface_treatment"], str(result["quantity"]) + " 件",
                      a["unit_price"] + " 元/件", a["total_price"] + " 元")) + " |", "",
                  "**报价总额：人民币 " + a["total_price"] + " 元。**", "",
                  "计价条件：未计税费，不含运费；税费和运费尚未计入。交期、付款条件及报价有效期均待约定。", "",
                  "包含口径：", ""]
        lines += ["- " + cell(x) for x in result["fee_inclusions"]]
        if result["engineering_limitations"]:
            lines += ["", "工程待核事项：", ""] + ["- " + cell(x) for x in result["engineering_limitations"]]
        lines += ["", "本稿待审核，不构成制造可行性确认或已批准的商务承诺。", ""]
        method = "成本加成率" if result["pricing_method"] == "cost_plus" else "目标毛利率"
        with localcontext() as context:
            context.prec = 64
            percent = format(number(result["pricing_rate"]) * 100, "f")
        def cost_display(raw):
            value = Decimal(raw)
            with localcontext() as context:
                context.prec = 64
                return money(value) if value == value.quantize(CENT, rounding=ROUND_HALF_UP) else format(value, "f")

        costs = ["# 内部成本核算", "", f"报价编号：{result['quote_id']} · 参数修订：{result['source_revision']}", "",
                 "| 成本项目 | 原始费率 | 计价量 | 金额（元，保留必要精度） |", "|---|---:|---:|---:|"]
        costs += ["| " + " | ".join(cell(x) for x in (x["operation"], x["rate"] + " " + x["rate_unit"],
                     x["pricing_quantity"] + " " + x["quantity_unit"], cost_display(x["raw_amount"]))) + " |" for x in result["cost_lines"]]
        costs += ["", "成本合计：" + a["total_cost"] + " 元；分摊单件成本：" + a["unit_cost"] + " 元/件。", "",
                  method + "：" + percent + "%；目标加成金额：" + a["markup_amount"] + " 元。", "",
                  "单价舍入调整：" + a["rounding_adjustment"] + " 元；未税单价：" + a["unit_price"] + " 元/件；未税、不含运总额：" + a["total_price"] + " 元。", "",
                  "成本加成按成本×(1+加成率)；目标毛利按成本÷(1−毛利率)。先以高精度汇总成本，再将单价四舍五入到分，总额=该单价×数量；舍入差额单列。", "",
                  "材料用量和费率的包含项沿用参数记录，不额外叠加损耗、人工、设备、耗材或包装。准备费用按单收取一次。", "",
                  "数据性质：" + ("模拟；工程未验证；待审核。" if result["simulated"] else "待审核；工程限制见报价草稿。"), "",
                  "原始记录 SHA-256：`" + result["source_record_sha256"] + "`", "",
                  "计价策略 SHA-256：`" + result["pricing_policy_sha256"] + "`", ""]
        return "\n".join(lines), "\n".join(costs)

    def issue(self, task_id, quote_id, policy_path, expected_revision):
        policy_path = Path(policy_path)
        require(policy_path.is_file(), "pricing policy file required")
        policy_bytes = policy_path.read_bytes()
        policy = json.loads(policy_bytes)
        with self.store.lock(task_id):
            record = self.store._load(task_id)
            require(type(expected_revision) is int and record["data_revision"] == expected_revision, "source revision conflict")
            record_bytes = (self.store.task_path(task_id) / "record.json").read_bytes()
            path = self._quote_path(task_id, quote_id)
            if path.exists():
                existing = self._load(task_id, quote_id)
                require(existing["source_record_sha256"] == sha(record_bytes) and existing["pricing_policy_sha256"] == sha(policy_bytes),
                        "quote id already exists with different inputs; use a new quote id")
                return existing
            result = self._calculate(record, policy)
            result.update(quote_id=quote_id, created_at=stamp(), source_record_sha256=sha(record_bytes), pricing_policy_sha256=sha(policy_bytes))
            path.parent.mkdir(mode=0o700, exist_ok=True)
            temporary = Path(tempfile.mkdtemp(prefix=".quote-", dir=path.parent))
            try:
                (temporary / "record_snapshot.json").write_bytes(record_bytes)
                (temporary / "pricing_policy.json").write_bytes(policy_bytes)
                (temporary / "quote.json").write_text(json_text(result), encoding="utf-8")
                quote_md, cost_md = self._documents(result)
                (temporary / "quote.md").write_text(quote_md, encoding="utf-8")
                (temporary / "cost_breakdown.md").write_text(cost_md, encoding="utf-8")
                for item in temporary.iterdir():
                    with item.open("rb") as stream:
                        os.fsync(stream.fileno())
                os.rename(temporary, path)
                fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
            result["source_current"] = True
            return result

    def render(self, task_id, quote_id):
        with self.store.lock(task_id):
            result = self._load(task_id, quote_id)
            quote_md, cost_md = self._documents(result)
            from prep_record import atomic_write
            path = self._quote_path(task_id, quote_id)
            atomic_write(path / "quote.md", quote_md)
            atomic_write(path / "cost_breakdown.md", cost_md)
            return {**self.paths(task_id, quote_id), "source_current": result["source_current"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("operation", choices=("issue", "show", "render"))
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--quote-id", required=True)
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--expected-revision", type=int)
    args = parser.parse_args(argv)
    try:
        store = QuoteStore(args.root)
        if args.operation == "issue":
            require(args.policy is not None, "--policy required")
            result = store.issue(args.task_id, args.quote_id, args.policy, args.expected_revision)
            result = {key: result[key] for key in ("quote_id", "status", "source_revision", "simulated", "source_current", "amounts")}
            result.update(store.paths(args.task_id, args.quote_id))
        elif args.operation == "render":
            result = store.render(args.task_id, args.quote_id)
        else:
            result = store.load(args.task_id, args.quote_id)
        print(json_text(result), end="")
        return 0
    except (RecordError, OSError, ValueError, KeyError, TypeError, DecimalException) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
