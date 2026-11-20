"""站务事件档案 (station event archive) —— 业务入口一。

为一次已确认运输登记可复核的 **事件偏序**。每条事件有：

* ``event_id`` 稳定编号；
* ``action`` 动作名（供许可规则匹配）；
* ``source_station`` 来源站；
* ``local_seq`` 该站严格递增的本地操作序号；
* ``[lo, hi]`` 业务时间所在闭区间（整数）。

先后关系 **只** 来自三处，绝不来自录入顺序：

1. 同一站按 ``local_seq`` 的先后；
2. 声明的显式交接 (``handoff``，即 “必须先于”);
3. 一条区间的上端 **严格小于** 另一条下端 (``hi_a < lo_b``)。

相接 (``hi_a == lo_b``) 与重叠本身都不证明先后。区间只作这条比较规则的
证据，站点并未保存可排序的精确时钟，因此本模块不会从时间值推断任何额外
次序。

磅单事件按该运输的进出两票绑定（``ticket`` 指向 IN/OUT）。挂车作业须引用
被引用的磅单，并沿其历史产品验证挂车身份——身份不符即 **错绑的实体**。

能力：

* ``event_order`` 给出可复核的偏序（事件、各类边、确定的拓扑序、传递闭包
  得到的全部被迫对）；
* ``event_order_report`` 额外把 “不可能成立的证据环” 作为数据返回而不抛错；
* ``add_handoff`` 补入新交接后重做，旧的档案与事件原样保留；
* 圈或错绑都在建立偏序时被拒——不会按录入顺序强行排出次序。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .partial_order import PartialOrder, PartialOrderError


class ArchiveError(ValueError):
    """事件档案输入或状态错误。"""


class EvidenceCycle(ArchiveError):
    """先后关系构成不可能成立的证据环。"""

    def __init__(self, cycle: list[str]) -> None:
        self.cycle = list(cycle)
        joined = " -> ".join(self.cycle + [self.cycle[0]] if self.cycle else [])
        super().__init__(f"evidence cycle: {joined}")


class TrailerMismatch(ArchiveError):
    """挂车作业引用的磅单历史产品上的挂车与声明不符 (错绑实体)。"""

    def __init__(self, event_id: str, declared: str | None, expected: str | None) -> None:
        self.event_id = event_id
        self.declared = declared
        self.expected = expected
        super().__init__(
            f"event {event_id} binds trailer {declared!r} but the referenced "
            f"weigh ticket's composition product carries {expected!r}"
        )


# 事件类别
KIND_WEIGH = "weigh"
KIND_TRAILER = "trailer"
KIND_OP = "op"

# 边来源
EDGE_STATION = "station"
EDGE_HANDOFF = "handoff"
EDGE_INTERVAL = "interval"


@dataclass
class _Event:
    event_id: str
    trip_id: str
    action: str
    station: str
    local_seq: int
    lo: int
    hi: int
    kind: str
    ticket_role: str | None = None
    references: str | None = None
    declared_trailer: str | None = None

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "action": self.action,
            "station": self.station,
            "local_seq": self.local_seq,
            "interval": [self.lo, self.hi],
            "kind": self.kind,
            "ticket": self.ticket_role,
            "references": self.references,
            "trailer": self.declared_trailer,
        }


@dataclass
class _Ticket:
    trip_id: str
    role: str
    ticket_id: str
    tractor: str
    trailer: str | None
    catalog_seq: int
    ts: int
    gross: int

    def to_dict(self) -> dict:
        return {
            "trip_id": self.trip_id,
            "role": self.role,
            "ticket_id": self.ticket_id,
            "tractor": self.tractor,
            "trailer": self.trailer,
            "catalog_seq": self.catalog_seq,
            "ts": self.ts,
            "gross": self.gross,
        }


@dataclass
class EventOrderReport:
    """可复核的事件偏序结果。"""

    trip_id: str
    events: list[dict]
    station_edges: list[tuple[str, str]]
    handoff_edges: list[tuple[str, str]]
    interval_edges: list[tuple[str, str]]
    edges: list[tuple[str, str]]
    topological: list[str]
    forced_pairs: list[tuple[str, str]]
    bindings: dict[str, dict]
    cycle: list[str] | None = None

    @property
    def ok(self) -> bool:
        return self.cycle is None

    def to_dict(self) -> dict:
        return {
            "trip_id": self.trip_id,
            "ok": self.ok,
            "cycle": self.cycle,
            "events": self.events,
            "edges": {
                "station": [list(e) for e in self.station_edges],
                "handoff": [list(e) for e in self.handoff_edges],
                "interval": [list(e) for e in self.interval_edges],
                "union": [list(e) for e in self.edges],
            },
            "topological": self.topological,
            "forced_pairs": [list(p) for p in self.forced_pairs],
            "bindings": self.bindings,
        }

    def to_partial_order(self) -> PartialOrder:
        """把已校验的偏序交给 *许可回放* 独立消费；有环时拒绝。"""
        if self.cycle is not None:
            raise EvidenceCycle(self.cycle)
        return PartialOrder(
            [(e["event_id"], e["action"]) for e in self.events], self.edges
        )


def _as_int(value: object, name: str) -> int:
    if isinstance(value, bool):
        raise ArchiveError(f"{name} must be an integer")
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ArchiveError(f"{name} must be an integer") from exc


class EventArchive:
    """一次运输的事件档案；可反复登记修正证据、交接与挂车身份。

    ``product_resolver`` 可选：``(tractor, catalog_seq) -> Product | None``（可
    直接传 :class:`weighbridge.Warehouse` 的 ``_product_for``，或任意按票面序号
    取历史组合产品的函数）。给了它，挂车身份会 **沿被引用磅单的历史产品** 核对，
    而非只看票面声明；没给则退回到票面声明的挂车。
    """

    def __init__(self, product_resolver=None) -> None:
        self._resolve_product = product_resolver
        self._trips: dict[str, dict] = {}
        self._tickets: dict[str, dict[str, _Ticket]] = {}   # trip -> role -> ticket
        self._events: dict[str, dict[str, _Event]] = {}     # trip -> event_id -> event
        self._handoffs: dict[str, list[tuple[str, str]]] = {}

    # ---------------------------------------------------------------- trips
    def add_trip(self, trip_id: str) -> None:
        trip_id = str(trip_id).strip()
        if not trip_id:
            raise ArchiveError("trip_id must be non-empty")
        self._trips.setdefault(trip_id, {})
        self._tickets.setdefault(trip_id, {})
        self._events.setdefault(trip_id, {})
        self._handoffs.setdefault(trip_id, [])

    def _require_trip(self, trip_id: str) -> None:
        if trip_id not in self._trips:
            raise ArchiveError(f"unknown archived trip {trip_id!r}")

    def trips(self) -> list[str]:
        return list(self._trips)

    # ---------------------------------------------------------------- tickets
    def add_ticket(
        self,
        trip_id: str,
        *,
        role: str,
        ticket_id: str,
        tractor: str,
        trailer: str | None,
        catalog_seq: int,
        ts: int,
        gross: int,
    ) -> _Ticket:
        self._require_trip(trip_id)
        role = str(role).upper()
        if role not in ("IN", "OUT"):
            raise ArchiveError(f"ticket role must be IN/OUT, got {role!r}")
        if role in self._tickets[trip_id]:
            raise ArchiveError(f"trip {trip_id} already has a {role} ticket")
        ticket = _Ticket(
            trip_id=trip_id, role=role, ticket_id=str(ticket_id),
            tractor=str(tractor), trailer=None if trailer is None else str(trailer),
            catalog_seq=_as_int(catalog_seq, "catalog_seq"),
            ts=_as_int(ts, "ts"), gross=_as_int(gross, "gross"),
        )
        self._tickets[trip_id][role] = ticket
        return ticket

    def tickets(self, trip_id: str) -> dict[str, dict]:
        self._require_trip(trip_id)
        return {role: t.to_dict() for role, t in self._tickets[trip_id].items()}

    # ---------------------------------------------------------------- events
    def add_event(
        self,
        trip_id: str,
        *,
        event_id: str,
        action: str,
        station: str,
        local_seq: int,
        lo: int,
        hi: int,
        ticket: str | None = None,
        references: str | None = None,
        trailer: str | None = None,
    ) -> _Event:
        """登记一条事件。

        * ``ticket``：磅单事件绑定本运输的 IN/OUT 票；
        * ``references``：挂车作业引用的磅单——可为角色 ``"IN"/"OUT"`` 或
          某一已登记事件的编号；沿被引用磅单的历史产品验证挂车身份；
        * ``trailer``：挂车作业声明的挂车稳定 ID。
        """
        self._require_trip(trip_id)
        event_id = str(event_id).strip()
        if not event_id:
            raise ArchiveError("event_id must be non-empty")
        if event_id in self._events[trip_id]:
            raise ArchiveError(f"duplicate event {event_id!r} in trip {trip_id}")
        action = str(action).strip()
        station = str(station).strip()
        if not action:
            raise ArchiveError(f"event {event_id} has an empty action")
        if not station:
            raise ArchiveError(f"event {event_id} has an empty source station")
        lo_i, hi_i = _as_int(lo, "lo"), _as_int(hi, "hi")
        if lo_i > hi_i:
            raise ArchiveError(f"event {event_id} has an empty interval [{lo_i},{hi_i}]")
        seq_i = _as_int(local_seq, "local_seq")
        if seq_i < 0:
            raise ArchiveError(f"event {event_id} local_seq must be >= 0")

        ref = None if references is None else str(references)
        role = None if ticket is None else str(ticket).upper()
        if role is not None and role not in self._tickets[trip_id]:
            raise ArchiveError(f"event {event_id} references unknown ticket role {role!r}")

        if ref is not None:
            kind = KIND_TRAILER
        elif role is not None:
            kind = KIND_WEIGH
        else:
            kind = KIND_OP
        # 引用的磅单可稍后登记 (仅校验结构: 要么是角色名, 要么非空编号)；
        # 真正的 “引用是否落到一张磅单” 在建立偏序时校验。
        if kind == KIND_TRAILER and not ref:
            raise ArchiveError(f"trailer event {event_id} needs a non-empty reference")

        event = _Event(
            event_id=event_id, trip_id=trip_id, action=action, station=station,
            local_seq=seq_i, lo=lo_i, hi=hi_i, kind=kind,
            ticket_role=role, references=ref,
            declared_trailer=None if trailer is None else str(trailer),
        )
        self._events[trip_id][event_id] = event
        return event

    def events(self, trip_id: str) -> list[dict]:
        self._require_trip(trip_id)
        return [e.to_dict() for e in self._sorted_events(trip_id)]

    def _sorted_events(self, trip_id: str) -> list[_Event]:
        return sorted(
            self._events[trip_id].values(),
            key=lambda e: (e.station, e.local_seq, e.event_id),
        )

    def _weigh_event_for_role(self, trip_id: str, role: str) -> _Event | None:
        matches = [
            e for e in self._events[trip_id].values()
            if e.kind == KIND_WEIGH and e.ticket_role == role
        ]
        if len(matches) > 1:
            raise ArchiveError(
                f"trip {trip_id} has multiple weigh events for {role} ticket"
            )
        return matches[0] if matches else None

    def _resolve_reference(self, trip_id: str, ref: str) -> _Event | None:
        """把引用解析为其磅单事件：角色名或事件编号（沿引用链回到磅单）。"""
        seen: set[str] = set()
        cur = str(ref)
        while True:
            if cur in ("IN", "OUT"):
                return self._weigh_event_for_role(trip_id, cur)
            ev = self._events[trip_id].get(cur)
            if ev is None:
                return None
            if ev.kind == KIND_WEIGH:
                return ev
            if ev.kind != KIND_TRAILER or ev.references is None:
                return None
            if cur in seen:
                return None
            seen.add(cur)
            cur = ev.references

    def binding_chain(self, trip_id: str, event_id: str) -> list[str]:
        """挂车作业回到其磅单事件的引用链（含两端）。"""
        self._require_trip(trip_id)
        chain: list[str] = []
        cur = event_id
        while True:
            ev = self._events[trip_id][cur]
            chain.append(cur)
            if ev.kind != KIND_TRAILER:
                chain.reverse()
                return chain
            cur = ev.references  # type: ignore[assignment]
            if cur in ("IN", "OUT"):
                we = self._weigh_event_for_role(trip_id, cur)
                if we is None:
                    raise ArchiveError(f"no weigh event for ticket role {cur}")
                chain.append(we.event_id)
                chain.reverse()
                return chain

    # ---------------------------------------------------------------- handoffs
    def add_handoff(self, trip_id: str, before: str, after: str) -> None:
        """补一条显式 “必须先于” 交接；重复登记幂等。"""
        self._require_trip(trip_id)
        evs = self._events[trip_id]
        if before not in evs or after not in evs:
            raise ArchiveError(f"handoff {before!r}->{after!r} references an unknown event")
        if before == after:
            raise ArchiveError("a handoff cannot relate an event to itself")
        pair = (str(before), str(after))
        if pair not in self._handoffs[trip_id]:
            self._handoffs[trip_id].append(pair)

    def handoffs(self, trip_id: str) -> list[tuple[str, str]]:
        self._require_trip(trip_id)
        return list(self._handoffs[trip_id])

    # ---------------------------------------------------------------- ordering
    def _station_edges(self, trip_id: str) -> list[tuple[str, str]]:
        by_station: dict[str, list[_Event]] = {}
        for ev in self._events[trip_id].values():
            by_station.setdefault(ev.station, []).append(ev)
        edges: list[tuple[str, str]] = []
        for station, evs in by_station.items():
            seqs = [e.local_seq for e in evs]
            if len(set(seqs)) != len(seqs):
                dup = sorted(s for s in set(seqs) if seqs.count(s) > 1)
                raise ArchiveError(
                    f"station {station!r} has duplicate local_seq {dup}; "
                    f"local operation numbers must strictly increase"
                )
            evs.sort(key=lambda e: e.local_seq)
            for a, b in zip(evs, evs[1:]):
                edges.append((a.event_id, b.event_id))
        return edges

    def _interval_edges(self, trip_id: str) -> list[tuple[str, str]]:
        evs = list(self._events[trip_id].values())
        edges: list[tuple[str, str]] = []
        for a in evs:
            for b in evs:
                if a.event_id == b.event_id:
                    continue
                # 上端严格小于下端才算证据；相接/重叠不算。
                if a.hi < b.lo:
                    edges.append((a.event_id, b.event_id))
        return edges

    def _historical_trailer(self, ticket: _Ticket) -> str | None:
        """沿磅单的历史组合产品取挂车；无 resolver 时回退到票面声明。"""
        if self._resolve_product is None:
            return ticket.trailer
        product = self._resolve_product(ticket.tractor, ticket.catalog_seq)
        if product is None:
            return ticket.trailer
        trailer_eid = getattr(product, "trailer_id", None)
        if trailer_eid is None:
            return None
        return trailer_eid.split(":", 1)[-1]

    def _bindings(self, trip_id: str) -> dict[str, dict]:
        """为每条事件给出其绑定的运输实体，并校验挂车身份。"""
        out: dict[str, dict] = {}
        for ev in self._sorted_events(trip_id):
            if ev.kind == KIND_WEIGH:
                t = self._tickets[trip_id][ev.ticket_role]  # type: ignore[index]
                out[ev.event_id] = {
                    "kind": KIND_WEIGH,
                    "ticket": t.ticket_id,
                    "role": t.role,
                    "tractor": t.tractor,
                    "trailer": t.trailer,
                    "trailer_from_history": self._historical_trailer(t),
                }
            elif ev.kind == KIND_TRAILER:
                we = self._resolve_reference(trip_id, ev.references)  # type: ignore[arg-type]
                if we is None:
                    raise ArchiveError(
                        f"trailer event {ev.event_id} references {ev.references!r}, "
                        f"which is not a weigh ticket event"
                    )
                t = self._tickets[trip_id][we.ticket_role]  # type: ignore[index]
                expected = self._historical_trailer(t)
                if ev.declared_trailer is not None and ev.declared_trailer != expected:
                    raise TrailerMismatch(ev.event_id, ev.declared_trailer, expected)
                out[ev.event_id] = {
                    "kind": KIND_TRAILER,
                    "ticket": t.ticket_id,
                    "role": t.role,
                    "tractor": t.tractor,
                    "trailer": expected,
                    "via": self.binding_chain(trip_id, ev.event_id),
                }
            else:
                out[ev.event_id] = {"kind": KIND_OP}
        return out

    def _build(self, trip_id: str) -> EventOrderReport:
        self._require_trip(trip_id)
        station_edges = self._station_edges(trip_id)
        handoff_edges = list(self._handoffs[trip_id])
        interval_edges = self._interval_edges(trip_id)

        # 挂车身份校验在建立偏序时进行：错绑直接拒绝。
        bindings = self._bindings(trip_id)

        all_edges = []
        for e in station_edges + handoff_edges + interval_edges:
            if e not in all_edges:
                all_edges.append(e)

        events = [e.to_dict() for e in self._sorted_events(trip_id)]
        order_events = [(e.event_id, e.action) for e in self._sorted_events(trip_id)]

        try:
            po = PartialOrder(order_events, all_edges)
        except PartialOrderError:
            # 用同一组边重新求一个具体环，作为可复核证据返回。
            cycle = self._find_cycle(trip_id, all_edges)
            return EventOrderReport(
                trip_id=trip_id, events=events,
                station_edges=station_edges, handoff_edges=handoff_edges,
                interval_edges=interval_edges, edges=all_edges,
                topological=[], forced_pairs=[],
                bindings=bindings, cycle=cycle,
            )

        topo = po.topological_sort()
        forced = self._forced_pairs(trip_id, all_edges, topo)
        return EventOrderReport(
            trip_id=trip_id, events=events,
            station_edges=station_edges, handoff_edges=handoff_edges,
            interval_edges=interval_edges, edges=all_edges,
            topological=topo, forced_pairs=forced,
            bindings=bindings, cycle=None,
        )

    def _find_cycle(self, trip_id: str, edges: list[tuple[str, str]]) -> list[str]:
        succ: dict[str, list[str]] = {eid: [] for eid in self._events[trip_id]}
        for a, b in edges:
            succ[a].append(b)
        color = {eid: 0 for eid in succ}
        stack: list[str] = []

        def dfs(u: str) -> list[str] | None:
            color[u] = 1
            stack.append(u)
            for v in sorted(succ[u]):
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
        return []

    def _forced_pairs(
        self, trip_id: str, edges: list[tuple[str, str]], topo: list[str]
    ) -> list[tuple[str, str]]:
        succ: dict[str, set[str]] = {eid: set() for eid in self._events[trip_id]}
        for a, b in edges:
            succ[a].add(b)
        reach: dict[str, set[str]] = {}
        for start in topo:
            seen: set[str] = set()
            stack = list(succ[start])
            while stack:
                u = stack.pop()
                if u in seen:
                    continue
                seen.add(u)
                stack.extend(succ[u])
            reach[start] = seen
        pairs: list[tuple[str, str]] = []
        for a in topo:
            for b in topo:
                if a != b and b in reach[a]:
                    pairs.append((a, b))
        return pairs

    def event_order_report(self, trip_id: str) -> EventOrderReport:
        """始终返回报告；证据环放在 ``cycle`` 字段而不是抛错。"""
        return self._build(trip_id)

    def event_order(self, trip_id: str) -> EventOrderReport:
        """返回可复核偏序；证据环抛 :class:`EvidenceCycle`。"""
        report = self._build(trip_id)
        if report.cycle is not None:
            raise EvidenceCycle(report.cycle)
        return report
