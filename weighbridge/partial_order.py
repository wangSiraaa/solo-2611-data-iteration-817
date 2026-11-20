"""可独立消费的事件偏序 (event partial order).

这是第 一 个业务入口 (*事件档案*) 与第 二 个业务入口 (*许可回放*) 之间唯一的
公共数据形状。它只认识 ``(event_id, action)`` 节点与有向 "必须先于" 边，
不含任何磅单、站点或令牌知识：

* 构造时就校验：编号唯一、动作非空、边只引用已知编号、无环。
* ``topological_sort()`` 给出确定的线性次序，供读者复核。
* ``predecessors()`` / ``successors()`` 提供直接邻居。

偏序只描述 "允许哪些完整线性次序"，绝不含录入顺序带来的隐含约束。
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from typing import Iterable, Sequence


class PartialOrderError(ValueError):
    """偏序本身不合法 (重名、悬空边、环)。"""


@dataclass(frozen=True)
class OrderEvent:
    """偏序中的一个节点：稳定编号 + 动作名 (动作名供许可规则匹配)。"""

    event_id: str
    action: str

    def to_dict(self) -> dict:
        return {"event_id": self.event_id, "action": self.action}


class PartialOrder:
    """经过校验的有向无环偏序。

    ``events`` 可以是 :class:`OrderEvent`、``dict`` 或 ``(event_id, action)``；
    ``edges`` 为 ``(before, after)`` 序列，表示 before 必须先于 after。
    """

    def __init__(
        self,
        events: Iterable[OrderEvent | dict | Sequence[str]],
        edges: Iterable[tuple[str, str] | dict] = (),
    ) -> None:
        norm: list[OrderEvent] = []
        seen: set[str] = set()
        for raw in events:
            if isinstance(raw, OrderEvent):
                ev = raw
            elif isinstance(raw, dict):
                ev = OrderEvent(str(raw["event_id"]), str(raw["action"]))
            else:
                eid, action = raw
                ev = OrderEvent(str(eid), str(action))
            if not ev.event_id.strip():
                raise PartialOrderError("event_id must be non-empty")
            if not ev.action.strip():
                raise PartialOrderError(f"event {ev.event_id} has an empty action")
            if ev.event_id in seen:
                raise PartialOrderError(f"duplicate event_id {ev.event_id!r}")
            seen.add(ev.event_id)
            norm.append(ev)
        self._events: tuple[OrderEvent, ...] = tuple(norm)
        self._by_id = {ev.event_id: ev for ev in self._events}

        pair_edges: list[tuple[str, str]] = []
        for raw in edges:
            if isinstance(raw, dict):
                before, after = str(raw["before"]), str(raw["after"])
            else:
                before, after = str(raw[0]), str(raw[1])
            if before not in self._by_id or after not in self._by_id:
                raise PartialOrderError(
                    f"edge {before!r}->{after!r} references an unknown event"
                )
            pair_edges.append((before, after))
        # 去重但保持确定性顺序
        self._edges: tuple[tuple[str, str], ...] = tuple(sorted(set(pair_edges)))

        self._succ: dict[str, set[str]] = {ev.event_id: set() for ev in self._events}
        self._pred: dict[str, set[str]] = {ev.event_id: set() for ev in self._events}
        for before, after in self._edges:
            self._succ[before].add(after)
            self._pred[after].add(before)

        if self._find_cycle() is not None:
            raise PartialOrderError("partial order contains a cycle")

    # ---------------------------------------------------------------- accessors
    def events(self) -> tuple[OrderEvent, ...]:
        return self._events

    def event_ids(self) -> tuple[str, ...]:
        return tuple(ev.event_id for ev in self._events)

    def edges(self) -> tuple[tuple[str, str], ...]:
        return self._edges

    def action_of(self, event_id: str) -> str:
        try:
            return self._by_id[event_id].action
        except KeyError as exc:
            raise PartialOrderError(f"unknown event {event_id!r}") from exc

    def predecessors(self) -> dict[str, frozenset[str]]:
        return {k: frozenset(v) for k, v in self._pred.items()}

    def successors(self) -> dict[str, frozenset[str]]:
        return {k: frozenset(v) for k, v in self._succ.items()}

    def _reach(self, start: str) -> set[str]:
        seen: set[str] = set()
        stack = list(self._succ[start])
        while stack:
            u = stack.pop()
            if u in seen:
                continue
            seen.add(u)
            stack.extend(self._succ[u])
        return seen

    def forces(self, before: str, after: str) -> bool:
        """偏序是否强制 before 先于 after（直接边或传递可达）。"""
        if before not in self._by_id or after not in self._by_id:
            raise PartialOrderError(f"unknown event in forces({before!r}, {after!r})")
        return after in self._reach(before)

    # ---------------------------------------------------------------- ordering
    def topological_sort(self) -> list[str]:
        """确定的线性次序：同时可放时按编号字典序取最小。"""
        indeg = {ev.event_id: len(self._pred[ev.event_id]) for ev in self._events}
        heap = [eid for eid, d in indeg.items() if d == 0]
        heapq.heapify(heap)
        out: list[str] = []
        while heap:
            u = heapq.heappop(heap)
            out.append(u)
            for v in sorted(self._succ[u]):
                indeg[v] -= 1
                if indeg[v] == 0:
                    heapq.heappush(heap, v)
        return out

    def _find_cycle(self) -> list[str] | None:
        color = {ev.event_id: 0 for ev in self._events}
        stack: list[str] = []

        def dfs(u: str) -> list[str] | None:
            color[u] = 1
            stack.append(u)
            for v in sorted(self._succ[u]):
                if color[v] == 1:
                    return stack[stack.index(v):]
                if color[v] == 0:
                    found = dfs(v)
                    if found is not None:
                        return found
            stack.pop()
            color[u] = 2
            return None

        for eid in sorted(color):
            if color[eid] == 0:
                found = dfs(eid)
                if found is not None:
                    return found
        return None

    # ---------------------------------------------------------------- output
    def to_dict(self) -> dict:
        return {
            "events": [ev.to_dict() for ev in self._events],
            "edges": [[a, b] for a, b in self._edges],
            "topological": self.topological_sort(),
        }
