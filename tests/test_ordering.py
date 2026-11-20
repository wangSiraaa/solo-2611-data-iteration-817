"""事件偏序 / 事件档案 / 许可回放的测试。

运行::

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import itertools
import unittest

from weighbridge import (
    Catalog,
    EventArchive,
    EvidenceCycle,
    OrderEvent,
    PartialOrder,
    PartialOrderError,
    PermitError,
    PermitScheme,
    TrailerMismatch,
    replay_permits,
)


# --------------------------------------------------------------- helpers
def all_linear_extensions(events, edges):
    """枚举满足偏序的全部完整线性次序。"""
    ids = [e.event_id for e in events]
    pred = {eid: set() for eid in ids}
    for a, b in edges:
        pred[b].add(a)
    out = []

    def rec(done: list[str], remaining: set[str]) -> None:
        if not remaining:
            out.append(list(done))
            return
        for eid in sorted(remaining):
            if pred[eid] <= set(done):
                rec(done + [eid], remaining - {eid})

    rec([], set(ids))
    return out


def simulate(seq, scheme):
    """按某一完整次序回放，返回 (违规数, 每步是否违规)。"""
    bal = {t.name: t.initial for t in scheme.tokens}
    caps = {t.name: t.capacity for t in scheme.tokens}
    count = 0
    flags: dict[str, bool] = {}
    for eid, action in seq:
        rule = scheme.rule_for(action)
        take = {n: c for n, c in rule.take}
        emit = {n: c for n, c in rule.emit}
        enough = all(bal[n] >= c for n, c in take.items())
        if enough:
            new = dict(bal)
            for n in set(take) | set(emit):
                new[n] = bal.get(n, 0) - take.get(n, 0) + emit.get(n, 0)
            within = all(new[n] <= caps[n] and new[n] >= 0 for n in new)
        else:
            within = False
        if enough and within:
            bal = new
            flags[eid] = False
        else:
            flags[eid] = True
            count += 1
    return count, flags


def brute_force(events, edges, scheme):
    seqs = all_linear_extensions(events, edges)
    values = []
    always: set[str] | None = None
    for order in seqs:
        seq = [(eid, dict((e.event_id, e.action) for e in events)[eid]) for eid in order]
        count, flags = simulate(seq, scheme)
        values.append(count)
        bad = {eid for eid, v in flags.items() if v}
        always = bad if always is None else (always & bad)
    return min(values), max(values), sorted(always)


# --------------------------------------------------------------- PartialOrder
class PartialOrderTests(unittest.TestCase):
    def test_validation_rejects_duplicates_and_dangling(self):
        with self.assertRaises(PartialOrderError):
            PartialOrder([("A", "x"), ("A", "y")])
        with self.assertRaises(PartialOrderError):
            PartialOrder([("A", "x")], [("A", "B")])
        with self.assertRaises(PartialOrderError):
            PartialOrder([("A", "x"), ("", "y")])
        with self.assertRaises(PartialOrderError):
            PartialOrder([("A", "")])

    def test_cycle_rejected(self):
        with self.assertRaises(PartialOrderError):
            PartialOrder([("A", "x"), ("B", "x"), ("C", "x")],
                         [("A", "B"), ("B", "C"), ("C", "A")])

    def test_topological_is_deterministic_and_valid(self):
        po = PartialOrder(
            [("C", "x"), ("A", "x"), ("B", "x"), ("D", "x")],
            [("A", "C"), ("B", "C")],
        )
        topo = po.topological_sort()
        self.assertEqual(set(topo), {"A", "B", "C", "D"})
        self.assertLess(topo.index("A"), topo.index("C"))
        self.assertLess(topo.index("B"), topo.index("C"))

    def test_edges_dedup(self):
        po = PartialOrder([("A", "x"), ("B", "x")], [("A", "B"), ("A", "B")])
        self.assertEqual(po.edges(), (("A", "B"),))


# --------------------------------------------------------------- EventArchive
def _archive_with_two_stations():
    a = EventArchive()
    a.add_trip("T")
    a.add_ticket("T", role="IN", ticket_id="K1", tractor="X", trailer="R1",
                 catalog_seq=1, ts=1, gross=100)
    a.add_ticket("T", role="OUT", ticket_id="K2", tractor="X", trailer="R1",
                 catalog_seq=1, ts=2, gross=200)
    a.add_event("T", event_id="W", action="weigh", station="S1",
                local_seq=1, lo=8, hi=9, ticket="IN")
    a.add_event("T", event_id="D", action="weigh", station="S2",
                local_seq=1, lo=8, hi=9, ticket="OUT")
    return a


class EventArchiveTests(unittest.TestCase):
    def test_same_station_strict_local_seq_orders(self):
        a = EventArchive()
        a.add_trip("T")
        a.add_ticket("T", role="IN", ticket_id="K1", tractor="X", trailer="R1",
                     catalog_seq=1, ts=1, gross=1)
        a.add_event("T", event_id="A", action="x", station="S", local_seq=1, lo=1, hi=9)
        a.add_event("T", event_id="B", action="x", station="S", local_seq=2, lo=1, hi=2)
        rep = a.event_order("T")
        # 同站本地序号决定先后；B 的时间区间并不更晚，但区间本身不反证。
        self.assertIn(("A", "B"), rep.edges)
        self.assertEqual(rep.topological, ["A", "B"])

    def test_duplicate_local_seq_on_same_station_rejected(self):
        a = EventArchive()
        a.add_trip("T")
        a.add_event("T", event_id="A", action="x", station="S", local_seq=1, lo=1, hi=1)
        a.add_event("T", event_id="B", action="x", station="S", local_seq=1, lo=2, hi=2)
        with self.assertRaises(Exception):
            a.event_order("T")

    def test_touching_and_overlap_do_not_prove_order(self):
        a = _archive_with_two_stations()
        rep = a.event_order("T")
        self.assertEqual(rep.interval_edges, [])
        self.assertEqual(rep.forced_pairs, [])
        # 两种次序都允许
        self.assertEqual(set(rep.topological), {"W", "D"})

    def test_strict_interval_end_does_prove_order(self):
        a = _archive_with_two_stations()
        a.add_event("T", event_id="EARLY", action="x", station="S3",
                    local_seq=1, lo=1, hi=7)
        rep = a.event_order("T")
        # EARLY.hi = 7 < W.lo = 8 -> 证明 EARLY 先于 W/D
        self.assertIn(("EARLY", "W"), rep.station_edges + rep.interval_edges)
        self.assertIn(("EARLY", "D"), rep.interval_edges)

    def test_insertion_order_does_not_force_anything(self):
        # 先登记 D 后登记 W，但两站各自序号独立，不得按录入顺序强排。
        a = EventArchive()
        a.add_trip("T")
        a.add_event("T", event_id="D", action="x", station="S2", local_seq=1, lo=8, hi=9)
        a.add_event("T", event_id="W", action="x", station="S1", local_seq=1, lo=8, hi=9)
        rep = a.event_order("T")
        self.assertEqual(rep.forced_pairs, [])

    def test_handoff_adds_order_and_redo_keeps_old_records(self):
        a = _archive_with_two_stations()
        before = a.event_order("T")
        self.assertEqual(before.forced_pairs, [])
        a.add_handoff("T", "D", "W")
        after = a.event_order("T")
        self.assertIn(("D", "W"), after.forced_pairs)
        # 旧证据仍在（事件、原边不丢失）
        self.assertEqual({e["event_id"] for e in after.events}, {"W", "D"})

    def test_handoff_cycle_reported_as_data_and_raises_on_strict_call(self):
        a = _archive_with_two_stations()
        a.add_handoff("T", "W", "D")
        a.add_handoff("T", "D", "W")
        rep = a.event_order_report("T")
        self.assertFalse(rep.ok)
        self.assertEqual(set(rep.cycle), {"W", "D"})
        with self.assertRaises(EvidenceCycle):
            a.event_order("T")

    def test_trailer_identity_follows_referenced_weigh_ticket(self):
        a = EventArchive()
        a.add_trip("T")
        a.add_ticket("T", role="IN", ticket_id="K1", tractor="X", trailer="R1",
                     catalog_seq=1, ts=1, gross=1)
        a.add_event("T", event_id="WB", action="weigh", station="S1",
                    local_seq=1, lo=1, hi=2, ticket="IN")
        a.add_event("T", event_id="OP", action="attach", station="S2",
                    local_seq=1, lo=3, hi=4, references="WB", trailer="R1")
        rep = a.event_order("T")
        self.assertEqual(rep.bindings["OP"]["trailer"], "R1")
        self.assertEqual(rep.bindings["OP"]["via"], ["WB", "OP"])

    def test_trailer_mismatch_raises(self):
        a = EventArchive()
        a.add_trip("T")
        a.add_ticket("T", role="IN", ticket_id="K1", tractor="X", trailer="R1",
                     catalog_seq=1, ts=1, gross=1)
        a.add_event("T", event_id="WB", action="weigh", station="S1",
                    local_seq=1, lo=1, hi=2, ticket="IN")
        a.add_event("T", event_id="OP", action="attach", station="S2",
                    local_seq=1, lo=3, hi=4, references="IN", trailer="R9")
        with self.assertRaises(TrailerMismatch) as ctx:
            a.event_order("T")
        self.assertEqual(ctx.exception.declared, "R9")
        self.assertEqual(ctx.exception.expected, "R1")

    def test_to_partial_order_carries_events_and_edges(self):
        a = _archive_with_two_stations()
        a.add_handoff("T", "W", "D")
        po = a.event_order("T").to_partial_order()
        self.assertEqual(po.edges(), (("W", "D"),))

    def test_forward_reference_resolved_at_build_time(self):
        a = EventArchive()
        a.add_trip("T")
        a.add_ticket("T", role="IN", ticket_id="K", tractor="X", trailer="R1",
                     catalog_seq=1, ts=1, gross=1)
        # 挂车作业先登记，引用稍后才登记的磅单事件。
        a.add_event("T", event_id="OP", action="a", station="S2",
                    local_seq=1, lo=3, hi=4, references="WB", trailer="R1")
        a.add_event("T", event_id="WB", action="w", station="S1",
                    local_seq=1, lo=1, hi=2, ticket="IN")
        rep = a.event_order("T")
        self.assertEqual(rep.bindings["OP"]["via"], ["WB", "OP"])

    def test_dangling_reference_rejected_at_build(self):
        a = EventArchive()
        a.add_trip("T")
        a.add_ticket("T", role="IN", ticket_id="K", tractor="X", trailer="R1",
                     catalog_seq=1, ts=1, gross=1)
        a.add_event("T", event_id="OP", action="a", station="S2",
                    local_seq=1, lo=3, hi=4, references="NOPE", trailer="R1")
        with self.assertRaises(Exception):
            a.event_order("T")

    def test_trailer_identity_from_catalog_history(self):
        # 用真实目录校验：宣言 R2，但票面序号处的历史产品挂的是 R1 -> 错绑。
        c = Catalog()
        c.define_mass("tractor", "T1", 7000, 7200)
        c.define_mass("trailer", "R1", 1500, 1600)
        c.define_mass("trailer", "R2", 2000, 2200)
        c.apply_attach("T1", "R1")   # seq 1
        c.apply_detach("T1", "R1")   # seq 2
        c.apply_attach("T1", "R2")   # seq 3

        a = EventArchive(product_resolver=lambda tr, seq: c.product_at(seq, tr))
        a.add_trip("T")
        a.add_ticket("T", role="IN", ticket_id="K", tractor="T1", trailer="R1",
                     catalog_seq=1, ts=1, gross=9000)
        a.add_event("T", event_id="WB", action="w", station="S1",
                    local_seq=1, lo=1, hi=2, ticket="IN")
        a.add_event("T", event_id="OP", action="a", station="S2",
                    local_seq=1, lo=3, hi=4, references="IN", trailer="R2")
        with self.assertRaises(TrailerMismatch) as ctx:
            a.event_order("T")
        self.assertEqual(ctx.exception.expected, "R1")


# --------------------------------------------------------------- Permit replay
def _scheme(tokens, rules):
    return PermitScheme(tokens=tokens, rules=rules)


class PermitReplayTests(unittest.TestCase):
    def test_spec_whitespace(self):
        # 不同站、区间均 [8,9] -> W→D 违规 0，D→W 违规 1。
        po = PartialOrder([("W", "W"), ("D", "D")])
        scheme = _scheme(
            [{"name": "P", "initial": 0, "capacity": 1}],
            [{"action": "W", "emit": {"P": 1}}, {"action": "D", "take": {"P": 1}}],
        )
        r = replay_permits(po, scheme)
        self.assertEqual((r.min_violations, r.max_violations), (0, 1))
        self.assertEqual(r.min_witness.order, ["W", "D"])
        self.assertEqual(r.max_witness.order, ["D", "W"])
        self.assertEqual(r.always_violating, [])

    def test_spec_fix_handoff_makes_d_always_violating(self):
        po = PartialOrder([("W", "W"), ("D", "D")], [("D", "W")])
        scheme = _scheme(
            [{"name": "P", "initial": 0, "capacity": 1}],
            [{"action": "W", "emit": {"P": 1}}, {"action": "D", "take": {"P": 1}}],
        )
        r = replay_permits(po, scheme)
        self.assertEqual((r.min_violations, r.max_violations), (1, 1))
        self.assertEqual(r.always_violating, ["D"])

    def test_spec_wider_scheme_makes_both_ok(self):
        po = PartialOrder([("W", "W"), ("D", "D")])
        scheme = _scheme(
            [{"name": "P", "initial": 1, "capacity": 2}],
            [{"action": "W", "emit": {"P": 1}}, {"action": "D", "take": {"P": 1}}],
        )
        r = replay_permits(po, scheme)
        self.assertEqual((r.min_violations, r.max_violations), (0, 0))
        self.assertEqual(r.always_violating, [])

    def test_spec_two_takes_no_fixed_violator(self):
        po = PartialOrder([("D1", "T"), ("D2", "T")])
        scheme = _scheme(
            [{"name": "P", "initial": 1, "capacity": 1}],
            [{"action": "T", "take": {"P": 1}}],
        )
        r = replay_permits(po, scheme)
        self.assertEqual((r.min_violations, r.max_violations), (1, 1))
        # 最少违规数 (1) 大于必然违规编号的数量 (0)。
        self.assertEqual(r.always_violating, [])
        self.assertGreater(r.min_violations, len(r.always_violating))

    def test_capacity_overflow_is_violation_and_state_kept(self):
        po = PartialOrder([("A", "W")])
        scheme = _scheme(
            [{"name": "P", "initial": 1, "capacity": 1}],
            [{"action": "W", "emit": {"P": 1}}],
        )
        r = replay_permits(po, scheme)
        self.assertEqual(r.min_violations, 1)
        step = r.min_witness.steps[0]
        self.assertTrue(step.violation)
        self.assertEqual(step.before, {"P": 1})
        self.assertEqual(step.after, {"P": 1})  # 全部牌数保持原状

    def test_witness_steps_balances_are_consistent(self):
        po = PartialOrder(
            [("A", "W"), ("B", "W"), ("C", "D"), ("D", "D")],
            [("A", "C")],
        )
        scheme = _scheme(
            [{"name": "P", "initial": 0, "capacity": 2},
             {"name": "Q", "initial": 1, "capacity": 3}],
            [
                {"action": "W", "emit": {"P": 1, "Q": 1}},
                {"action": "D", "take": {"P": 1}, "emit": {"Q": 1}},
            ],
        )
        r = replay_permits(po, scheme)
        for w in (r.min_witness, r.max_witness):
            self.assertEqual(w.steps[0].before, {t.name: t.initial for t in scheme.tokens})
            for prev, step in zip(w.steps, w.steps[1:]):
                # 干净执行才改变余额；违规动作余额保持原状。
                self.assertEqual(step.before, prev.after)
            for step in w.steps:
                if step.violation:
                    self.assertEqual(step.before, step.after)
            # 见证次序必须满足偏序
            pos = {eid: i for i, eid in enumerate(w.order)}
            for a, b in po.edges():
                self.assertLess(pos[a], pos[b])

    def test_matches_brute_force_over_all_extensions(self):
        cases = [
            (PartialOrder([("W", "W"), ("D", "D")], [("D", "W")]),
             _scheme([{"name": "P", "initial": 0, "capacity": 1}],
                     [{"action": "W", "emit": {"P": 1}},
                      {"action": "D", "take": {"P": 1}}])),
            (PartialOrder([("D1", "T"), ("D2", "T")]),
             _scheme([{"name": "P", "initial": 1, "capacity": 1}],
                     [{"action": "T", "take": {"P": 1}}])),
            (PartialOrder([("A", "W"), ("B", "W"), ("C", "D"), ("D", "D")]),
             _scheme([{"name": "P", "initial": 0, "capacity": 2}],
                     [{"action": "W", "emit": {"P": 1}},
                      {"action": "D", "take": {"P": 1}}])),
            (PartialOrder(
                [("A", "X"), ("B", "Y"), ("C", "X"), ("D", "Y")],
                [("A", "D"), ("B", "C")]),
             _scheme([{"name": "P", "initial": 0, "capacity": 3},
                      {"name": "Q", "initial": 0, "capacity": 3}],
                     [{"action": "X", "emit": {"P": 1}},
                      {"action": "Y", "emit": {"Q": 1}},
                      {"action": "Z", "take": {"P": 1, "Q": 1}}])),
        ]
        for po, scheme in cases:
            events = po.events()
            bmin, bmax, balways = brute_force(events, po.edges(), scheme)
            r = replay_permits(po, scheme)
            self.assertEqual((r.min_violations, r.max_violations), (bmin, bmax))
            self.assertEqual(r.always_violating, balways)

    def test_capacity_bounds_rejected(self):
        with self.assertRaises(PermitError):
            _scheme([{"name": "P", "initial": 0, "capacity": 4}], [])
        with self.assertRaises(PermitError):
            _scheme(
                [{"name": f"P{i}", "initial": 0, "capacity": 1} for i in range(4)],
                [],
            )
        with self.assertRaises(PermitError):
            _scheme([{"name": "P", "initial": 2, "capacity": 1}], [])

    def test_unknown_action_rejected(self):
        po = PartialOrder([("A", "NOPE")])
        scheme = _scheme([{"name": "P", "initial": 1, "capacity": 1}],
                         [{"action": "W", "emit": {"P": 1}}])
        with self.assertRaises(PermitError):
            replay_permits(po, scheme)

    def test_event_count_bound_enforced(self):
        po = PartialOrder([(f"e{i}", "W") for i in range(15)])
        scheme = _scheme([{"name": "P", "initial": 0, "capacity": 3}],
                         [{"action": "W", "emit": {"P": 1}}])
        with self.assertRaises(PermitError):
            replay_permits(po, scheme)

    def test_fourteen_events_three_tokens_capacity_three(self):
        # 边界内最大规模：14 事件、3 类牌、容量 3，应能求解且见证合法。
        ids = [f"e{i:02d}" for i in range(14)]
        edges = [(ids[i], ids[i + 1]) for i in range(0, 13, 2)]  # 稀疏约束
        po = PartialOrder([(e, "W" if i % 2 else "D") for i, e in enumerate(ids)], edges)
        scheme = _scheme(
            [{"name": f"P{i}", "initial": 1, "capacity": 3} for i in range(3)],
            [{"action": "W", "emit": {"P0": 1, "P1": 1}},
             {"action": "D", "take": {"P0": 1}, "emit": {"P2": 1}}],
        )
        r = replay_permits(po, scheme)
        self.assertEqual(len(r.min_witness.order), 14)
        self.assertEqual(len(r.max_witness.order), 14)
        pos = {eid: i for i, eid in enumerate(r.min_witness.order)}
        for a, b in po.edges():
            self.assertLess(pos[a], pos[b])

    def test_scheme_reuse_on_same_order(self):
        # 同一份偏序可被不同许可方案重复消费。
        po = PartialOrder([("W", "W"), ("D", "D")])
        narrow = _scheme([{"name": "P", "initial": 0, "capacity": 1}],
                         [{"action": "W", "emit": {"P": 1}},
                          {"action": "D", "take": {"P": 1}}])
        wide = _scheme([{"name": "P", "initial": 1, "capacity": 2}],
                       [{"action": "W", "emit": {"P": 1}},
                        {"action": "D", "take": {"P": 1}}])
        self.assertEqual(replay_permits(po, narrow).max_violations, 1)
        self.assertEqual(replay_permits(po, wide).max_violations, 0)


class IntegrationTests(unittest.TestCase):
    def test_archive_order_feeds_replay(self):
        a = _archive_with_two_stations()  # 事件 W 与 D，互不分先后
        po = a.event_order("T").to_partial_order()
        scheme = _scheme(
            [{"name": "P", "initial": 0, "capacity": 1}],
            [{"action": "weigh", "emit": {"P": 1}}],
        )
        r = replay_permits(po, scheme)
        # 两个都发同一张牌、容量 1：两种次序都各违规一次，无固定必违规。
        self.assertEqual((r.min_violations, r.max_violations), (1, 1))
        self.assertEqual(r.always_violating, [])


if __name__ == "__main__":
    unittest.main()
