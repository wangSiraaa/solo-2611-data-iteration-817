"""班组操作许可回放 (shift permit replay) —— 业务入口二。

班组许可按 **动作** 配置可重复使用的通行牌规则：

* 每类牌有初始数 ``initial`` 与容量 ``capacity``（余额永不超过容量）；
* 每个动作要求先取回若干指定牌 (``take``) 再发出若干牌 (``emit``)。

回放给定的一份合法偏序（见 :mod:`weighbridge.partial_order`）：它只约束
“哪些完整线性次序是被允许的”，绝不从录入顺序补链。对 **所有遵守该偏序的
完整线性次序**：

* ``min_violations`` / ``max_violations``：违规动作数的最少 / 最多值；
* 各给一份完整见证（事件次序 + 逐步余额）；
* ``always_violating``：在每一种合法次序下都违规的具体事件编号。

一次动作若 “取回后余额不足” 或 “执行后某类牌余额超过容量”，记一次违规，
该动作走过，但 **所有牌数保持原样**。

搜索方式：在所有 *可达序理想* (order ideal) 上做动态规划。余额只取决于
“已执行哪些事件” 这个下闭集合，与到达它的路径无关，因此同一理想的不同
路径可合并——这使 14 个事件也只需对至多 ``2^14`` 个理想各扫描一次。
最少违规数可能大于 “必然违规编号” 的数量：两个互无先后的取牌动作共争一张
牌时，每个次序都恰好有一项违规，但没有任何一个固定事件在两序中都违规。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .partial_order import PartialOrder, OrderEvent

MAX_EVENTS = 14
MAX_TOKEN_TYPES = 3
MAX_CAPACITY = 3

STATUS_OK = "ok"
STATUS_VIOLATION = "violation"


class PermitError(ValueError):
    """许可方案、偏序或容量边界不合法。"""


# ----------------------------------------------------------------- 配置
@dataclass(frozen=True)
class TokenType:
    name: str
    initial: int
    capacity: int

    def to_dict(self) -> dict:
        return {"name": self.name, "initial": self.initial, "capacity": self.capacity}


@dataclass(frozen=True)
class PermitRule:
    action: str
    take: tuple[tuple[str, int], ...]
    emit: tuple[tuple[str, int], ...]

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "take": {name: c for name, c in self.take},
            "emit": {name: c for name, c in self.emit},
        }


def _norm_counts(raw: object, where: str) -> list[tuple[str, int]]:
    if raw is None:
        return []
    if isinstance(raw, dict):
        items = raw.items()
    else:
        items = raw  # type: ignore[assignment]
    out: list[tuple[str, int]] = []
    for name, count in items:
        if isinstance(count, bool) or int(count) != count:
            raise PermitError(f"{where}: count for {name!r} must be an integer")
        count = int(count)
        if count < 0:
            raise PermitError(f"{where}: count for {name!r} must be >= 0")
        if count:
            out.append((str(name), count))
    return out


class PermitScheme:
    """一套通行牌方案；与具体事件偏序无关，可复用于多份偏序。"""

    def __init__(
        self,
        tokens: Iterable[dict],
        rules: Iterable[dict],
        *,
        max_events: int = MAX_EVENTS,
        max_token_types: int = MAX_TOKEN_TYPES,
        max_capacity: int = MAX_CAPACITY,
    ) -> None:
        self.max_events = int(max_events)
        self.max_token_types = int(max_token_types)
        self.max_capacity = int(max_capacity)

        types: list[TokenType] = []
        seen: set[str] = set()
        for raw in tokens:
            name = str(raw["name"]).strip()
            if not name:
                raise PermitError("token name must be non-empty")
            if name in seen:
                raise PermitError(f"duplicate token type {name!r}")
            seen.add(name)
            initial = int(raw.get("initial", 0))
            capacity = int(raw.get("capacity", 0))
            if initial < 0 or capacity < 0:
                raise PermitError(f"token {name!r} counts must be >= 0")
            if initial > capacity:
                raise PermitError(
                    f"token {name!r} initial {initial} exceeds capacity {capacity}"
                )
            if capacity > self.max_capacity:
                raise PermitError(
                    f"token {name!r} capacity {capacity} exceeds bound {self.max_capacity}"
                )
            types.append(TokenType(name, initial, capacity))
        if len(types) > self.max_token_types:
            raise PermitError(
                f"at most {self.max_token_types} token types, got {len(types)}"
            )
        self.tokens = types
        self._tok_index = {t.name: i for i, t in enumerate(types)}

        self.rules: dict[str, PermitRule] = {}
        for raw in rules:
            action = str(raw["action"]).strip()
            if not action:
                raise PermitError("rule action must be non-empty")
            if action in self.rules:
                raise PermitError(f"duplicate rule for action {action!r}")
            take = self._normalize(raw.get("take"), action, "take")
            emit = self._normalize(raw.get("emit"), action, "emit")
            self.rules[action] = PermitRule(
                action,
                tuple(sorted(take.items())),
                tuple(sorted(emit.items())),
            )

    def _normalize(self, raw: object, action: str, which: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for name, count in _norm_counts(raw, f"rule {action!r} {which}"):
            if name not in self._tok_index:
                raise PermitError(
                    f"rule {action!r} {which} references unknown token {name!r}"
                )
            counts[name] = counts.get(name, 0) + count
        return counts

    # 向量化，供回放内部使用
    def _vectors(self, rule: PermitRule) -> tuple[list[int], list[int]]:
        k = len(self.tokens)
        take = [0] * k
        emit = [0] * k
        for name, c in rule.take:
            take[self._tok_index[name]] += c
        for name, c in rule.emit:
            emit[self._tok_index[name]] += c
        return take, emit

    @property
    def initial_balance(self) -> tuple[int, ...]:
        return tuple(t.initial for t in self.tokens)

    @property
    def capacities(self) -> tuple[int, ...]:
        return tuple(t.capacity for t in self.tokens)

    def rule_for(self, action: str) -> PermitRule:
        try:
            return self.rules[action]
        except KeyError as exc:
            raise PermitError(f"no permit rule for action {action!r}") from exc

    def describe(self) -> dict:
        return {
            "tokens": [t.to_dict() for t in self.tokens],
            "rules": [r.to_dict() for r in self.rules.values()],
        }


# ----------------------------------------------------------------- 回放
@dataclass
class WitnessStep:
    event_id: str
    action: str
    status: str
    violation: bool
    before: dict[str, int]
    after: dict[str, int]
    reason: str

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "action": self.action,
            "status": self.status,
            "violation": self.violation,
            "before": self.before,
            "after": self.after,
            "reason": self.reason,
        }


@dataclass
class Witness:
    kind: str  # "min" | "max"
    violations: int
    order: list[str]
    steps: list[WitnessStep]

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "violations": self.violations,
            "order": list(self.order),
            "steps": [s.to_dict() for s in self.steps],
        }


@dataclass
class PermitReplay:
    min_violations: int
    max_violations: int
    min_witness: Witness
    max_witness: Witness
    always_violating: list[str]
    event_count: int
    ideal_count: int
    state_count: int
    tokens: list[dict]
    rules: list[dict]
    order: dict

    def to_dict(self) -> dict:
        return {
            "min_violations": self.min_violations,
            "max_violations": self.max_violations,
            "always_violating": list(self.always_violating),
            "min_witness": self.min_witness.to_dict(),
            "max_witness": self.max_witness.to_dict(),
            "event_count": self.event_count,
            "ideal_count": self.ideal_count,
            "state_count": self.state_count,
            "tokens": self.tokens,
            "rules": self.rules,
            "order": self.order,
        }


class PermitReplayEngine:
    """在一份许可方案上回放若干事件偏序。"""

    def __init__(self, scheme: PermitScheme) -> None:
        self.scheme = scheme

    # ------------------------------------------------------------ core search
    def replay(self, order: PartialOrder) -> PermitReplay:
        scheme = self.scheme
        events: tuple[OrderEvent, ...] = tuple(
            sorted(order.events(), key=lambda e: e.event_id)
        )
        n = len(events)
        if n == 0:
            raise PermitError("partial order has no events")
        if n > scheme.max_events:
            raise PermitError(
                f"at most {scheme.max_events} events per replay, got {n}"
            )
        event_ids = [e.event_id for e in events]
        idx = {eid: i for i, eid in enumerate(event_ids)}
        rules = [scheme.rule_for(e.action) for e in events]
        take_vec = [scheme._vectors(r)[0] for r in rules]
        emit_vec = [scheme._vectors(r)[1] for r in rules]
        caps = scheme.capacities

        pred = [0] * n
        for before, after in order.edges():
            pred[idx[after]] |= 1 << idx[before]

        k = len(scheme.tokens)
        init = scheme.initial_balance
        full = (1 << n) - 1

        def transitions(mask: int, balance: tuple[int, ...]):
            for i in range(n):
                bit = 1 << i
                if mask & bit:
                    continue
                if (pred[i] & mask) != pred[i]:
                    continue
                take = take_vec[i]
                emit = emit_vec[i]
                enough = all(balance[t] >= take[t] for t in range(k))
                new = [balance[t] - take[t] + emit[t] for t in range(k)]
                within = all(0 <= new[t] <= caps[t] for t in range(k))
                if enough and within:
                    yield i, tuple(new), False, ""
                elif not enough:
                    yield i, balance, True, "insufficient tokens to take"
                else:
                    yield i, balance, True, "capacity exceeded after emit"

        # 1) 搜索状态 = (已执行事件集合 mask, 当前余额向量)。
        #
        # 关键：违规动作 “走过但牌数原样”，因此同一 mask 经不同路径可能落到
        # 不同余额——余额 *并非* mask 的函数。把余额并入状态才正确。状态数上界
        # 仍是 2^n × ∏(capacity+1)，在 ≤14 事件、≤3 类、容量 ≤3 的题面边界内
        # 很小，且只枚举满足偏序的 mask (可达理想)。
        State = tuple[int, tuple[int, ...]]
        start: State = (0, init)

        # 前向可达状态集合。
        states: set[State] = {start}
        frontier = [start]
        while frontier:
            mask, bal = frontier.pop()
            for i, newbal, _viol, _reason in transitions(mask, bal):
                nxt = (mask | (1 << i), newbal)
                if nxt not in states:
                    states.add(nxt)
                    frontier.append(nxt)

        if not any(mask == full for mask, _ in states):
            raise PermitError("no linear extension exists (should be impossible)")

        # 2) 逆向 DP：每个状态到 full 的最少 / 最多违规。
        popcount = {m: bin(m).count("1") for m, _ in states}
        order_states = sorted(states, key=lambda s: (-popcount[s[0]], s[0], s[1]))
        best_lo: dict[State, int] = {s: 0 for s in states if s[0] == full}
        best_hi: dict[State, int] = {s: 0 for s in states if s[0] == full}
        for state in order_states:
            mask, bal = state
            if mask == full:
                continue  # 终态已种子为 0
            lo: int | None = None
            hi: int | None = None
            for i, newbal, viol, _reason in transitions(mask, bal):
                nxt = (mask | (1 << i), newbal)
                if nxt not in best_lo:
                    continue
                v = 1 if viol else 0
                cand_lo = v + best_lo[nxt]
                cand_hi = v + best_hi[nxt]
                lo = cand_lo if lo is None else min(lo, cand_lo)
                hi = cand_hi if hi is None else max(hi, cand_hi)
            if lo is None:
                raise PermitError("internal: state has no continuation")
            best_lo[state] = lo
            best_hi[state] = hi

        # 3) 见证重建。
        min_witness = self._build_witness(
            "min", best_lo, transitions, event_ids, rules, start, full
        )
        max_witness = self._build_witness(
            "max", best_hi, transitions, event_ids, rules, start, full
        )

        # 4) 必然违规：不存在任何合法次序允许它 “干净” 执行。
        #
        # 动作 i 能否干净执行，取决于排在它之前的动作实际怎么排；而余额在违规
        # 后会分叉。因此对每个可达状态看 i 能否干净执行一次——只要存在某条
        # 合法路径的某个前缀让 i 干净执行，i 就非必然违规。若所有可达状态都不能
        # 干净执行 i，则 i 在每一种合法次序下都违规。这能区分 “最少违规数 >
        # 必违规编号数” 的情形（两个取牌动作共争一张牌时无固定必违规事件）。
        always: list[str] = []
        for i in range(n):
            bit = 1 << i
            clean_possible = False
            for mask, bal in states:
                if mask & bit:
                    continue
                if (pred[i] & mask) != pred[i]:
                    continue
                if any(tr[0] == i and not tr[2] for tr in transitions(mask, bal)):
                    clean_possible = True
                    break
            if not clean_possible:
                always.append(event_ids[i])
        always.sort(key=lambda eid: event_ids.index(eid))

        return PermitReplay(
            min_violations=best_lo[start],
            max_violations=best_hi[start],
            min_witness=min_witness,
            max_witness=max_witness,
            always_violating=always,
            event_count=n,
            ideal_count=len({mask for mask, _ in states}),
            state_count=len(states),
            tokens=[t.to_dict() for t in scheme.tokens],
            rules=[r.to_dict() for r in scheme.rules.values()],
            order=order.to_dict(),
        )

    def _build_witness(
        self,
        kind: str,
        best: dict[tuple[int, tuple[int, ...]], int],
        transitions,
        event_ids: list[str],
        rules: list[PermitRule],
        start: tuple[int, tuple[int, ...]],
        full: int,
    ) -> Witness:
        state = start
        steps: list[WitnessStep] = []
        order_ids: list[str] = []
        violations = 0
        scheme = self.scheme
        names = [t.name for t in scheme.tokens]
        while state[0] != full:
            mask, bal = state
            target = best[state]
            chosen = None
            for tr in transitions(mask, bal):
                i, newbal, viol, reason = tr[0], tr[1], tr[2], tr[3]
                nxt = (mask | (1 << i), newbal)
                v = 1 if viol else 0
                if nxt in best and v + best[nxt] == target:
                    chosen = (i, newbal, viol, reason, nxt)
                    break
            if chosen is None:
                raise PermitError("internal: witness reconstruction failed")
            i, newbal, viol, reason, nxt = chosen
            before = dict(zip(names, bal))
            after = dict(zip(names, newbal))
            steps.append(
                WitnessStep(
                    event_id=event_ids[i],
                    action=rules[i].action,
                    status=STATUS_VIOLATION if viol else STATUS_OK,
                    violation=viol,
                    before=before,
                    after=after,
                    reason=reason if viol else _ok_reason(rules[i]),
                )
            )
            order_ids.append(event_ids[i])
            violations += 1 if viol else 0
            state = nxt
        return Witness(kind=kind, violations=violations, order=order_ids, steps=steps)


def _ok_reason(rule: PermitRule) -> str:
    takes = ", ".join(f"{n}x{c}" for n, c in rule.take) or "none"
    emits = ", ".join(f"{n}x{c}" for n, c in rule.emit) or "none"
    return f"took [{takes}] then emitted [{emits}] within bounds"


def replay_permits(order: PartialOrder, scheme: PermitScheme) -> PermitReplay:
    """便捷函数：在一份偏序上回放一套许可方案。"""
    return PermitReplayEngine(scheme).replay(order)
