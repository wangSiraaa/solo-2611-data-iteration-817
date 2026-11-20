"""站务事件偏序与班组许可回放的可复跑合成演示。

运行::

    python3 -m demo.run_ordering          # 打印事件档案与许可回放结果
    python3 -m demo.run_ordering --json   # 另写 demo/out/ordering_*.json

覆盖题面要求的案例：

* 两个不同站的事件时间区间均为 [8,9]、无交接 -> 偏序互不分先后；
* 令牌初始 0 / 容量 1、W 发牌、D 取牌：W→D 违规 0 次，D→W 违规 1 次；
* 固定原许可、补入 “D 先于 W” 的交接 -> 最少最多均为 1，D 必然违规；
* 保留原偏序、仅把许可改为初始 1 / 容量 2 -> 两种次序都合规；
* 两个互无先后的取牌动作共争一张牌 -> 每次恰有一项违规，但无必然违规事件；
* 更正证据：挂车错绑被拒；证据环被作为数据给出；
* 沿被引用磅单的历史产品验证挂车身份。
"""

from __future__ import annotations

import argparse
import json
import pathlib

from weighbridge import (
    Catalog,
    EventArchive,
    EvidenceCycle,
    PermitScheme,
    TrailerMismatch,
    replay_permits,
)


def _scheme(tokens, rules) -> PermitScheme:
    return PermitScheme(tokens=tokens, rules=rules)


def build_archive() -> EventArchive:
    """一次运输的事件档案：两站磅单 + 跨站挂车作业 + 一条显式交接。"""
    a = EventArchive()
    a.add_trip("TRIP-100")
    # 该运输的进出两票；换挂：入厂挂 R-A，出厂挂 R-B。
    a.add_ticket("TRIP-100", role="IN", ticket_id="K-IN", tractor="T-101",
                 trailer="R-A", catalog_seq=1, ts=1000, gross=9000)
    a.add_ticket("TRIP-100", role="OUT", ticket_id="K-OUT", tractor="T-101",
                 trailer="R-B", catalog_seq=3, ts=1400, gross=12800)

    # 两台磅站各自留下磅单：时间区间相连/重叠，本身不证明先后。
    a.add_event("TRIP-100", event_id="WB-IN", action="weigh", station="ST-A",
                local_seq=1, lo=950, hi=1050, ticket="IN")
    a.add_event("TRIP-100", event_id="WB-OUT", action="weigh", station="ST-B",
                local_seq=1, lo=1380, hi=1420, ticket="OUT")
    # 挂车作业：引用磅单，并声明挂车；身份沿历史产品核对。
    # ATT-RA 与 WB-IN 的时间区间重叠，故只有显式交接能证明其先后。
    a.add_event("TRIP-100", event_id="ATT-RA", action="attach", station="ST-YARD",
                local_seq=1, lo=940, hi=960, references="IN", trailer="R-A")
    a.add_event("TRIP-100", event_id="DET-RB", action="detach", station="ST-YARD",
                local_seq=2, lo=1450, hi=1500, references="OUT", trailer="R-B")
    # 显式交接：进厂磅单必须先于挂车作业 (只加一条可复核的约束)。
    a.add_handoff("TRIP-100", "WB-IN", "ATT-RA")
    return a


def build_shared_archive() -> EventArchive:
    """两个不同站的事件区间均为 [8,9]，且暂无交接。"""
    a = EventArchive()
    a.add_trip("SHARED")
    a.add_ticket("SHARED", role="IN", ticket_id="S-IN", tractor="T-9",
                 trailer="R-9", catalog_seq=1, ts=8, gross=9000)
    a.add_ticket("SHARED", role="OUT", ticket_id="S-OUT", tractor="T-9",
                 trailer="R-9", catalog_seq=1, ts=9, gross=12000)
    a.add_event("SHARED", event_id="W", action="W", station="ST-1",
                local_seq=1, lo=8, hi=9, ticket="IN")
    a.add_event("SHARED", event_id="D", action="D", station="ST-2",
                local_seq=1, lo=8, hi=9, ticket="OUT")
    return a


def build_broken_archive() -> tuple[EventArchive, dict]:
    """更正证据：先制造错绑，再制造证据环，各自给出可复核结论。"""
    a = EventArchive()
    a.add_trip("BAD")
    a.add_ticket("BAD", role="IN", ticket_id="B-IN", tractor="T-1",
                 trailer="R-1", catalog_seq=1, ts=1, gross=9000)
    a.add_event("BAD", event_id="WB", action="weigh", station="ST-1",
                local_seq=1, lo=1, hi=2, ticket="IN")
    # 声明 R-2，但磅单历史产品上挂的是 R-1 -> 错绑实体。
    a.add_event("BAD", event_id="OP", action="attach", station="ST-2",
                local_seq=1, lo=3, hi=4, references="IN", trailer="R-2")
    mismatch = None
    try:
        a.event_order("BAD")
    except TrailerMismatch as exc:
        mismatch = {"event_id": exc.event_id, "declared": exc.declared,
                    "expected": exc.expected}

    # 证据环：两台站各自区间给出相反先后，再补一条交接闭环。
    b = EventArchive()
    b.add_trip("CYC")
    b.add_ticket("CYC", role="IN", ticket_id="C-IN", tractor="T-1",
                 trailer="R-1", catalog_seq=1, ts=1, gross=9000)
    b.add_ticket("CYC", role="OUT", ticket_id="C-OUT", tractor="T-1",
                 trailer="R-1", catalog_seq=1, ts=9, gross=12000)
    b.add_event("CYC", event_id="P", action="w", station="ST-1",
                local_seq=1, lo=1, hi=1, ticket="IN")
    b.add_event("CYC", event_id="Q", action="w", station="ST-2",
                local_seq=1, lo=5, hi=5, ticket="OUT")
    b.add_handoff("CYC", "Q", "P")  # 与 P.hi=1 < Q.lo=5 的区间证据相反
    report = b.event_order_report("CYC")
    cycle_raised = None
    try:
        b.event_order("CYC")
    except EvidenceCycle as exc:
        cycle_raised = exc.cycle
    return a, {
        "mismatch": mismatch,
        "cycle_report_ok": report.ok,
        "cycle": report.cycle,
        "cycle_raised": cycle_raised,
        "interval_edges": [list(e) for e in report.interval_edges],
        "handoff_edges": [list(e) for e in report.handoff_edges],
    }


def _witness_json(w) -> dict:
    return w.to_dict()


def build_catalog_backed_archive() -> tuple[EventArchive, dict]:
    """挂车身份沿真实目录的历史产品核对（而非只看票面声明）。"""
    c = Catalog()
    c.define_mass("tractor", "T-101", 7050, 7150)
    c.define_mass("trailer", "R-A", 1550, 1650)
    c.define_mass("trailer", "R-B", 2100, 2250)
    c.apply_attach("T-101", "R-A")   # seq 1 -> (T-101, R-A)
    c.apply_detach("T-101", "R-A")   # seq 2
    c.apply_attach("T-101", "R-B")   # seq 3 -> (T-101, R-B)

    a = EventArchive(product_resolver=lambda tr, seq: c.product_at(seq, tr))
    a.add_trip("TRIP-200")
    a.add_ticket("TRIP-200", role="IN", ticket_id="C-IN", tractor="T-101",
                 trailer="R-A", catalog_seq=1, ts=1, gross=9000)
    a.add_event("TRIP-200", event_id="WB-IN", action="weigh", station="ST-A",
                local_seq=1, lo=1, hi=2, ticket="IN")
    # 错误声明 R-B：seq 1 的历史产品其实是 R-A -> 错绑被拒。
    a.add_event("TRIP-200", event_id="BAD", action="attach", station="ST-YARD",
                local_seq=1, lo=3, hi=4, references="IN", trailer="R-B")
    err = None
    try:
        a.event_order("TRIP-200")
    except TrailerMismatch as exc:
        err = {"event_id": exc.event_id, "declared": exc.declared,
               "expected_from_history": exc.expected}
    return a, {"mismatch": err}


def build_ordering_report() -> dict:
    out: dict = {}

    # 1) 含磅单与挂车作业的完整档案。
    archive = build_archive()
    report = archive.event_order("TRIP-100")
    out["archive"] = report.to_dict()

    # 1b) 目录支撑的挂车身份核对：错绑给出 “历史产品上到底是哪台挂车”。
    _, catalog_backed = build_catalog_backed_archive()
    out["catalog_backed_mismatch"] = catalog_backed

    # 2) 不同站、区间均 [8,9]、无交接 -> 互不证明先后。
    shared = build_shared_archive()
    shared_order = shared.event_order("SHARED")
    out["shared_order"] = {
        "topological": shared_order.topological,
        "forced_pairs": [list(p) for p in shared_order.forced_pairs],
        "interval_edges": [list(e) for e in shared_order.interval_edges],
    }
    po_free = shared_order.to_partial_order()

    # W 发一张、D 取一张；初始 0、容量 1。
    base_scheme = _scheme(
        [{"name": "P", "initial": 0, "capacity": 1}],
        [{"action": "W", "emit": {"P": 1}}, {"action": "D", "take": {"P": 1}}],
    )
    out["free_permits"] = replay_permits(po_free, base_scheme).to_dict()

    # 3) 固定许可，补 “D 先于 W” 交接 -> 最少最多均 1，D 必然违规。
    shared.add_handoff("SHARED", "D", "W")
    fixed_order = shared.event_order("SHARED")
    out["fixed_order"] = {
        "topological": fixed_order.topological,
        "handoff_edges": [list(e) for e in fixed_order.handoff_edges],
    }
    out["fixed_permits"] = replay_permits(
        fixed_order.to_partial_order(), base_scheme
    ).to_dict()

    # 4) 保留原偏序，仅改许可：初始 1、容量 2 -> 两种次序都合规。
    wider_scheme = _scheme(
        [{"name": "P", "initial": 1, "capacity": 2}],
        [{"action": "W", "emit": {"P": 1}}, {"action": "D", "take": {"P": 1}}],
    )
    out["widened_permits"] = replay_permits(po_free, wider_scheme).to_dict()

    # 5) 两个互无先后的取牌动作共争一张牌。
    contest = EventArchive()
    contest.add_trip("CONTEST")
    contest.add_ticket("CONTEST", role="IN", ticket_id="X-IN", tractor="T-2",
                       trailer="R-2", catalog_seq=1, ts=1, gross=9000)
    contest.add_ticket("CONTEST", role="OUT", ticket_id="X-OUT", tractor="T-2",
                       trailer="R-2", catalog_seq=1, ts=2, gross=12000)
    contest.add_event("CONTEST", event_id="T1", action="TAKE", station="ST-1",
                      local_seq=1, lo=1, hi=2, ticket="IN")
    contest.add_event("CONTEST", event_id="T2", action="TAKE", station="ST-2",
                      local_seq=1, lo=1, hi=2, ticket="OUT")
    contest_scheme = _scheme(
        [{"name": "P", "initial": 1, "capacity": 1}],
        [{"action": "TAKE", "take": {"P": 1}}],
    )
    out["contest_permits"] = replay_permits(
        contest.event_order("CONTEST").to_partial_order(), contest_scheme
    ).to_dict()

    # 6) 更正证据：错绑与证据环。
    _, broken = build_broken_archive()
    out["evidence_correction"] = broken
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="写出 demo/out/ordering_*.json")
    args = ap.parse_args()

    data = build_ordering_report()
    print(json.dumps(data, indent=2, ensure_ascii=False))

    if args.json:
        out = pathlib.Path(__file__).parent / "out"
        out.mkdir(exist_ok=True)
        (out / "ordering_report.json").write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"\nwrote {out}/ordering_report.json", flush=True)


if __name__ == "__main__":
    main()
