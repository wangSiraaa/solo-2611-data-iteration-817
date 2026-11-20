# 挂车换接下的双向过磅与净装载事实仓

一台牵引车第一次带空挂车进厂、第二次却换了另一挂车出厂时，若按车牌配对两次
毛重，会把两套不同空车质量混在一起，制造不存在的净货重。本仓库把问题拆成
两个互相独立的模块：

* **车辆组合目录** (`weighbridge/catalog.py`)：只管车辆稳定身份、空车质量整数
  区间 `[lo, hi]`（kg）以及按严格递增序号发生的组合动作。它不知道磅单。
* **过磅事实仓** (`weighbridge/warehouse.py`)：消费目录在票面序号处的 *公开组合
  产品*，把毛重换算成货重区间、组织运输、计算净装卸量的精确区间、处理重评。

全部输入为本地合成整数记录，不连接地磅或车牌识别设备。Python 3.11，仅用标准
库（SQLite + `fractions.Fraction`）。

## 快速开始

```bash
python3 -m unittest discover -s tests -v     # 75 个测试
python3 -m demo.run_demo                     # 合成案例
python3 -m demo.run_ordering                 # 事件偏序 / 许可回放案例
python3 -m demo.run_demo --json              # 另写 demo/out/*.json
python3 -m demo.run_ordering --json          # 另写 demo/out/ordering_*.json
```

```python
from weighbridge import Catalog, Warehouse, LOAD, IN, OUT

c = Catalog()
c.define_mass("tractor", "T1", 7000, 7200)   # 整数 kg，lo <= hi
c.define_mass("trailer", "R1", 1500, 1600)
c.define_mass("trailer", "R2", 2000, 2200)

c.apply_attach("T1", "R1")        # seq 1
c.apply_detach("T1", "R1")        # seq 2
c.apply_attach("T1", "R2")        # seq 3
print(c.product_at(1, "T1").stable_identity())   # ('T1', 'R1')，历史不被换挂覆盖

w = Warehouse(c)
w.add_ticket(dict(ticket_id="IN",  tractor="T1", catalog_seq=1, ts=100,
                  direction=IN,  gross=9000))
w.add_ticket(dict(ticket_id="OUT", tractor="T1", catalog_seq=3, ts=200,
                  direction=OUT, gross=12000))

ev = w.preview(LOAD, "IN", "OUT")  # DRAFT：只算，不占票
print(ev.net_lo, ev.net_hi)        # 2300 2600（整数空重区间上的精确极值）
w.confirm(ev.trip_id)              # CONFIRMED：此刻才占票
print(w.reconcile())               # [] 表示核对通过
```

## 目录语义

* **稳定身份**：实体 ID 为 `tractor:<id>` / `trailer:<id>`，永不复用。
* **动作序号**：`attach` / `detach` / `set_mass` 的序号严格递增；失败不消耗
  序号。**批动作** `apply_batch` 在单事务内执行，任一步失败整批回滚（序号、
  绑定、产品、批次记录全部不存在）。
* **组合约束**：一个挂车同时至多一台牵引车；一台牵引车同时至多一个挂车。
  由两枚 `WHERE detach_seq IS NULL` 的部分唯一索引强制。
* **组合产品**：每次 `attach`/`detach` 产生一个 `EMPTY` 产品；`set_mass` 产生
  `RELABEL` 产品。产品带 `relation_src`（`initial`/`attach`/`detach`/
  `mass_correction`）、`attach_seq`、`predecessor`。
* **历史不被覆盖**：产品的质量是落盘时的快照。查询任意序号：

  ```python
  c.product_at(seq, tractor)   # 该序号处已生效的 live 产品
  c.history("trailer", "R1")   # 该实体参与过的全部产品（含更正版）
  c.snapshot_at(seq, tractor)  # 可跨系统交换的 JSON 快照
  c.corrected_product(p)       # 同一挂接 + attach_seq 的最新更正版
  ```

  换挂之后，旧序号处的旧产品原样保留；`corrected_product` 只沿同一
  `(identity, attach_seq)` 的 `predecessor` 链走，绝不从 "当前挂接" 猜历史。

## 仓库语义

**单票货重**：`tare` 为组合产品各实体空车质量之和，
`goods = gross - tare`，可行区间
`[max(0, gross - tare_hi), gross - tare_lo]`；`gross < tare_lo`（上界为负）时该票
不合物理约束。`explain_ticket(tid)` 给出毛重、`tare`、`goods`、每个实体区间与
宽度——即 "这张票为什么是这个货重、哪些实体贡献了不确定性"。

**运输**：`preview(kind, ticket_in, ticket_out)` 显式选定一 IN 一 OUT 票，绝不按
时间就近猜配。IN 时间不得晚于 OUT；两票必须同车；一票不能同时占两个角色。
结果状态：

| 状态 | 含义 |
| --- | --- |
| `DRAFT` | 预览，未占票；失败时为不可确认的草稿 |
| `CONFIRMED` | 已确认；两票写进 `confirmed_ticket`，此后不可被别的运输占用 |
| `REVIEW` | 对已确认运输的重开/重评 |
| `SUPERSEDED` | 未确认的旧草稿/复核被同族新结果取代 |

**净装卸量**：

* 组合完全相同（同一牵引车 + 同一挂车）：空车质量是同一未知量，在毛重差中
  **精确抵消**，`net == |gross_out - gross_in|`，与区间宽度无关。
* 换挂：共享实体（如牵引车）系数为 0，精确抵消；不同实体以目录区间参与。
  在 "两张票货重非负 + 运输方向" 的联合可行域上，对净装卸量做**精确整数分支
  定界**（实数 LP 松弛用有理数顶点枚举），输出最小/最大净装卸量与货流区间。
  可行域为空则拒绝，区分 `DIRECTION_IMPOSSIBLE` 与 `TICKET_INFEASIBLE`。
* 绝不使用区间中点或实数顶点作为结算值。

`contributions` 逐个实体给出 `role`（`cancelled`/`fixed`/`uncertain`）、线性系数与
区间宽度。

**重评与重开**：

```python
c.apply_set_mass("trailer", "R1", 1500, 1600)  # 目录迟到更正
re = w.reopen(trip_id)        # 新增 REVIEW 运输，带 predecessor_trip
w.evaluation_history(re.trip_id)  # 旧（CONFIRMED）与新（REVIEW）都在，链式可追溯
```

* 已确认运输及其评估是历史事实，不会被覆盖或标为 `superseded`。
* 被确认运输占用的票不会被重新记成另一运输（重评沿用同一票与角色）。
* 重评默认对原评估中的 *历史产品* 取 `corrected_product`，而不是套用当前挂接。

**外部组合快照**：`Warehouse(None)` 可完全不依赖目录运行；用
`register_snapshot(...)` 导入与目录相同 JSON 格式的快照，按票面序号取产品。
若某张票的序号没有对应快照，直接报错——绝不回退到当前挂接去猜。

**核对**：`w.reconcile()`（`w.verify()` 同义，含目录自检）返回不一致列表，覆盖：
确认运输的两票方向/同车/时间序与可行性、确认占票唯一、每条评估在存下的快照
上可重算、同身份精确抵消、重评前序存在。

## 容量

目录 100 实体（`define_mass` 时强制）；仓库 1000 张票、200 次运输。同一车同一秒
可有多张不同 `ticket_id` 的票。

## 站务事件档案 (业务入口一) 与班组许可回放 (业务入口二)

两个可独立调用的入口构建在同一份、不含业务语义的 **事件偏序**
(`weighbridge/partial_order.py`) 之上：

* `PartialOrder(events, edges)`：校验重名 / 悬空边 / 环，可给确定的拓扑序与
  传递闭包；`events` 为 `(event_id, action)`，`edges` 为 “必须先于”。

### 事件档案 `EventArchive`

为一次运输登记事件（编号 / 动作 / 来源站 / 该站严格递增的本地序号 /
`[lo,hi]` 业务时间区间）。先后关系 **只** 来自三处，绝不来自录入顺序：

1. 同一站按 `local_seq`（同站重号直接拒绝）；
2. 显式交接 `add_handoff(before, after)`；
3. 一条区间上端 **严格小于** 另一条下端（相接 / 重叠都不算）。

```python
from weighbridge import EventArchive, EvidenceCycle, TrailerMismatch

a = EventArchive()
a.add_trip("TRIP-1")
a.add_ticket("TRIP-1", role="IN",  ticket_id="K-IN",  tractor="T1",
             trailer="R1", catalog_seq=1, ts=1000, gross=9000)
a.add_ticket("TRIP-1", role="OUT", ticket_id="K-OUT", tractor="T1",
             trailer="R2", catalog_seq=3, ts=1400, gross=12800)
a.add_event("TRIP-1", event_id="WB-IN", action="weigh", station="ST-A",
            local_seq=1, lo=950, hi=1050, ticket="IN")     # 磅单事件绑票
a.add_event("TRIP-1", event_id="ATT", action="attach", station="YARD",
            local_seq=1, lo=940, hi=960, references="IN", trailer="R1")
a.add_handoff("TRIP-1", "WB-IN", "ATT")

rep = a.event_order("TRIP-1")          # 有环则抛 EvidenceCycle
print(rep.topological, rep.forced_pairs, rep.bindings)
a.add_handoff("TRIP-1", "ATT", "WB-IN") # 补入后重做：旧事件/旧边保留
a.event_order_report("TRIP-1").cycle    # 证据环作为数据返回，不抛错
```

* 磅单事件按该运输的进出两票绑定（`ticket="IN"/"OUT"`）。
* 挂车作业须 `references` 一张磅单（角色名或事件编号，可链式）并声明挂车；
  给了 `EventArchive(product_resolver=...)`（可直接是 `Catalog.product_at`）
  就 **沿被引用磅单的历史产品** 核对挂车身份，否则按票面声明核对。身份不符抛
  `TrailerMismatch(declared, expected)`——即 **错绑的实体**。
* `event_order_report` 总是返回报告，把环放进 `cycle`；`event_order` 有环即抛
  `EvidenceCycle`。两者都绝不按录入顺序强行排序。
* `rep.to_partial_order()` 把已校验的偏序交给许可回放。

### 许可回放 `PermitScheme` + `PermitReplayEngine`

按动作配置可重复使用的通行牌：每类牌有 `initial` 与 `capacity`；每个动作
`take` 若干牌再 `emit` 若干牌。回放对所有 **遵守给定偏序的完整线性次序** 求：

* `min_violations` / `max_violations` 及各自的完整 `min_witness` / `max_witness`
  （事件次序 + 每步 `before`/`after` 余额）；
* `always_violating`：每种合法次序下都违规的具体事件编号。

一次动作若取回后余额不足、或执行后超出容量，记一次违规，**该动作走过但全部
牌数保持原状**。方案与偏序解耦：改许可只需 `replay_permits(order, scheme)`；
偏序不变也可换方案重算。

```python
from weighbridge import PartialOrder, PermitScheme, replay_permits

po = PartialOrder([("W", "W"), ("D", "D")])   # 两站、无交接
scheme = PermitScheme(
    tokens=[{"name": "P", "initial": 0, "capacity": 1}],
    rules=[{"action": "W", "emit": {"P": 1}},
           {"action": "D", "take": {"P": 1}}],
)
r = replay_permits(po, scheme)
print(r.min_violations, r.max_violations)     # 0 1（W→D 合规，D→W 违规）
r = replay_permits(PartialOrder([("W","W"),("D","D")], [("D","W")]), scheme)
print(r.min_violations, r.max_violations, r.always_violating)  # 1 1 ['D']
# 只改许可（初始 1、容量 2），同一偏序两种次序都合规
r = replay_permits(po, PermitScheme(
    tokens=[{"name": "P", "initial": 1, "capacity": 2}],
    rules=[{"action": "W", "emit": {"P": 1}},
           {"action": "D", "take": {"P": 1}}]))
print(r.min_violations, r.max_violations)     # 0 0
```

**搜索方式**：状态 = `(已执行事件的下闭集合, 余额向量)`。违规动作不改余额，
故同一集合经不同路径可落到不同余额，余额必须并入状态才正确。状态上界
`2^14 × ∏(capacity+1) ≤ 2^14 × 4^3`，只枚举满足偏序的集合；`min`/`max` 用逆向
DP，`always_violating` 用 “该动作在任一可达状态能否干净执行” 判定。边界：每个
运输至多 14 个事件、3 类牌、每类容量至多 3（超出即拒绝）。

**“最少违规数 > 必然违规编号数”**：两个互无先后的取牌动作共争一张牌时，每个
次序恰有一项违规（`min==max==1`），但没有任何固定事件必违规（`always_violating==[]`）。

## 文件

```
weighbridge/catalog.py        独立车辆组合目录
weighbridge/warehouse.py      过磅事实仓 + 精确整数求解
weighbridge/partial_order.py  校验过的 DAG 偏序（两个入口共用）
weighbridge/event_archive.py  入口一：站务事件档案与可复核偏序
weighbridge/permit_replay.py  入口二：班组许可回放 (min/max/必然违规)
weighbridge/__init__.py       公共 API
tests/test_weighbridge.py     46 个测试（含小整数区间暴力核对极值）
tests/test_ordering.py        29 个测试（含全线性扩展暴力核对 min/max）
demo/run_demo.py              可复跑合成案例（原）
demo/run_ordering.py          可复跑偏序 / 许可回放案例
demo/out/                     演示输出（--json 时生成）
```

## 覆盖的案例 (偏序与许可)

* 同站严格本地序号成链；跨站相接/重叠不证明先后；区间上端严格小于下端才成边；
* 不同站、两事件区间均 `[8,9]`：W→D 违规 0、D→W 违规 1；
* 固定许可补入 “D 先于 W” 交接 → 最少最多均 1、D 必然违规；
* 保留原偏序、仅将许可改为初始 1/容量 2 → 两种次序都合规；
* 两个互无先后的取牌动作共争一张牌 → 每次一项违规、无固定必违规事件；
* 证据环：`event_order` 抛 `EvidenceCycle`、`event_order_report` 作为数据返回；
* 错绑挂车：按票面声明核对，或用真实目录沿历史产品核对；
* 全线性扩展暴力核对 `min`/`max`/`always_violating`（含 800 例随机交叉验证）。

