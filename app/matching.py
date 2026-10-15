"""用整数分计算目标金额上下两侧最接近的 0/1 发票组合。

首组保证总额最优；其余组通过有限次数排除首组选中票生成，供用户比较，
不承诺是全部组合中的全局第二、第三名。未使用金额舍入或近似搜索。
"""
from __future__ import annotations

from math import gcd, isqrt
from functools import reduce

MAX_CANDIDATES = 1000
MAX_TARGET_CENTS = 10_000_000


def _solve(
    items: list[tuple[object, int]], target: int, direction: str
) -> tuple[list[object], int]:
    if not items or target <= 0:
        return [], 0
    single = None
    if direction == "below":
        items = [item for item in items if item[1] <= target]
        if not items:
            return [], 0
        upper = min(target, sum(amount for _, amount in items))
    else:
        # 达标的大额单票单独比较，避免按其票面金额分配 bitset。
        single = min((item for item in items if item[1] >= target),
                     key=lambda item: item[1], default=None)
        items = [item for item in items if item[1] < target]
        if sum(amount for _, amount in items) < target:
            return ([single[0]], single[1]) if single else ([], 0)
        # 小额票首次跨过目标时，合计至多为 target + 最大票额 - 1。
        upper = target + max(amount for _, amount in items) - 1
        if single:
            upper = min(upper, single[1])
    scale = reduce(gcd, (amount for _, amount in items))
    capacity = upper // scale
    weights = [amount // scale for _, amount in items]
    mask = (1 << (capacity + 1)) - 1
    # 块检查点与块内回溯都仅保留 O(sqrt(n)) 个 bitset。
    block = max(1, isqrt(len(items)))
    checkpoints = [1]
    reachable = 1
    for index, weight in enumerate(weights):
        reachable |= (reachable << weight) & mask
        if (index + 1) % block == 0:
            checkpoints.append(reachable)
    if direction == "below":
        best = reachable.bit_length() - 1
    else:
        minimum = (target + scale - 1) // scale
        above = reachable >> minimum
        if not above:
            return ([single[0]], single[1]) if single else ([], 0)
        best = minimum + (above & -above).bit_length() - 1
    remaining = best
    selected = []
    end = len(items)
    while end and remaining:
        start = ((end - 1) // block) * block
        local = [checkpoints[start // block]]
        for index in range(start, end):
            previous = local[-1]
            local.append(previous | ((previous << weights[index]) & mask))
        for index in range(end - 1, start - 1, -1):
            before = local[index - start]
            if (before >> remaining) & 1:
                continue
            selected.append(items[index][0])
            remaining -= weights[index]
        end = start
    selected.reverse()
    return selected, best * scale


def best_combinations(
    invoices: list[dict], target_cents: int, limit: int = 3, *, direction: str = "above"
) -> list[dict]:
    """首组金额最优：above 不少于目标，below 不超过目标。

    返回 invoice_ids / total_cents / gap_cents；gap_cents 为目标减合计。
    """
    if direction not in ("above", "below"):
        raise ValueError("匹配方式须为稍多或稍少")
    if isinstance(target_cents, bool) or not isinstance(target_cents, int):
        raise ValueError("目标金额必须为整数分")
    if not 0 <= target_cents <= MAX_TARGET_CENTS:
        raise ValueError("目标金额须在 0 至 100,000 元之间")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10:
        raise ValueError("建议组合数须在 1 至 10 之间")
    candidates = []
    seen_ids = set()
    positive_count = 0
    for invoice in invoices:
        amount = invoice.get("amount_cents", 0)
        if isinstance(amount, bool) or not isinstance(amount, int):
            raise ValueError("发票金额必须为整数分")
        if amount <= 0:
            continue
        positive_count += 1
        if positive_count > MAX_CANDIDATES:
            raise ValueError("一次最多匹配 1,000 张发票，请先缩小筛选范围")
        invoice_id = invoice.get("id")
        if not isinstance(invoice_id, (str, int)) or isinstance(invoice_id, bool):
            raise ValueError("发票缺少有效编号")
        if invoice_id in seen_ids:
            raise ValueError("候选发票编号重复")
        seen_ids.add(invoice_id)
        candidates.append((invoice_id, amount))
    first_ids, first_total = _solve(candidates, target_cents, direction)
    if not first_ids:
        return []
    results = [{"invoice_ids": first_ids, "total_cents": first_total,
                "gap_cents": target_cents - first_total}]
    if limit == 1:
        return results
    seen = {frozenset(first_ids)}
    # 有限搜索限制响应时间；均匀挑选首组中的票来产生不同建议。
    trials = min(len(first_ids), max(12, limit * 2))
    excluded_ids = [first_ids[index * len(first_ids) // trials] for index in range(trials)]
    alternatives = []
    for excluded_id in excluded_ids:
        chosen, total = _solve([item for item in candidates if item[0] != excluded_id], target_cents, direction)
        key = frozenset(chosen)
        if not chosen or key in seen:
            continue
        seen.add(key)
        alternatives.append({"invoice_ids": chosen, "total_cents": total,
                             "gap_cents": target_cents - total})
        # 同额组合已经达到最优，无须为了凑齐搜索次数继续计算。
        if sum(item["total_cents"] == first_total for item in alternatives) >= limit - 1:
            break
    alternatives.sort(key=lambda item: item["total_cents"], reverse=direction == "below")
    return results + alternatives[:limit - 1]
