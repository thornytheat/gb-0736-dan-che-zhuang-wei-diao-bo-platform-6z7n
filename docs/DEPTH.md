# 单车桩位调拨台规则与测试索引

- 规模为中型。供站点调度员复查早高峰尝试、核算未成交并试算补车方向。
- 同一批有效尝试以发生时间进入无调拨基准和候选方案。动态容量、关站暂存、车载在途及在骑车辆进入同一守恒账。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 桩事件收件 | 上传事件或核对隔离、迟报和同号矛盾 | `GET /api/intake` `POST /api/intake` | `app/modules/intake/service.py` `app/modules/intake/reader.py` | `tests/test_intake.py` |
| M2 容量版本 | 核对每站有效容量和冲突维修单 | `GET /api/capacity` | `app/modules/capacity/versions.py` `app/modules/capacity/service.py` | `tests/test_capacity.py` |
| M3 借还回放 | 按时点查看可借、可还、暂存状态 | `GET /api/replay` | `app/modules/replay/service.py` `app/modules/replay/schedule.py` | `tests/test_replay.py` |
| M4 溢出需求 | 按站、时槽和原因追踪未成交 | `GET /api/demand` | `app/modules/demand/service.py` `app/modules/demand/buckets.py` | `tests/test_demand.py` |
| M5 调拨试算 | 验证行程并比较逐站、逐笔差量 | `GET /api/dispatch` `POST /api/dispatch/preview` `POST /api/dispatch/publish` | `app/modules/dispatch/constraints.py` `app/modules/dispatch/compare.py` | `tests/test_dispatch.py` |
| M6 库存勾稽 | 核对各站期末及全网车辆守恒 | `GET /api/inventory` | `app/modules/inventory/service.py` `app/modules/inventory/stations.py` | `tests/test_inventory.py` |
| M7 班次方案留档 | 幂等重跑、回切和下载归档证据 | `GET /api/runs` `POST /api/runs/rerun` `POST /api/runs/restore` `GET /api/runs/<run_id>/evidence.json` | `app/modules/archive/service.py` `app/modules/archive/evidence.py` `app/store.py` | `tests/test_archive.py` |
| M8 暴雨补结 | 凭现场凭证按实际执行段补结中断调拨，逐站核算可借车与未成交，支持重开核对 | `GET /api/settlement` `POST /api/settlement/preview` `POST /api/settlement/publish` `POST /api/settlement/restore` `GET /api/settlement/<id>/evidence.json` | `app/modules/settlement/service.py` `app/modules/settlement/replay.py` `app/modules/settlement/vouchers.py` `app/store.py` | `tests/test_settlement.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现会怎样 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-01 | GB18030 导出含中文 | UTF-8 解码失败 | UTF-8 严格解码，失败回退 GB18030，测试提供带中文的字节样例 | `app/modules/intake/reader.py` | `tests/test_intake.py::test_gb18030_reordered_columns_and_mixed_line_endings` |
| TRAP-02 | `events.csv` 首列带 BOM | 首列无法匹配模板 | 解码时移除 BOM 并记录编码 | `app/modules/intake/reader.py` | `tests/test_intake.py::test_bom_and_leading_zero` |
| TRAP-03 | `events.csv` 的 0007 含跨行备注 | 拆坏记录和后续源行 | CSV 引号规则和物理起始行 | `app/modules/intake/reader.py` | `tests/test_intake.py::test_quoted_multiline_keeps_physical_source_rows` |
| TRAP-04 | `events.csv` 设备恢复后带分段表头 | 表头被当事件 | 用 `REPEATED_HEADER` 隔离 | `app/modules/intake/reader.py` | `tests/test_intake.py::test_segment_header_and_extra_short_rows_are_isolated` |
| TRAP-05 | `events.csv` 的 0095 缺列 | 数据错列 | 用 `COLUMN_COUNT` 隔离 | `app/modules/intake/reader.py` | `tests/test_intake.py::test_segment_header_and_extra_short_rows_are_isolated` |
| TRAP-07 | `events.csv` 的 0093 时间为 `-` | 错当缺省时间 | 缺失判定先于时间解析 | `app/modules/intake/normalize.py` | `tests/test_intake.py::test_sentinel_is_never_zero` |
| TRAP-08 | 站号 `001` 与事件号 `0001` | 转数字丢前导零 | 按字符串留存和归并 | `app/modules/intake/normalize.py` | `tests/test_intake.py::test_bom_and_leading_zero` |
| TRAP-11 | `events.csv` 的 0002 以两种偏移上报 | 被重复扣车 | UTC 归一后按业务键合并 | `app/modules/intake/service.py` | `tests/test_intake.py::test_offset_duplicate_keeps_earliest_received` |
| TRAP-13 | `events.csv` 的 0011 迟到 13 分 15 秒 | 0012 错判缺车 | 按发生时间回放并保留迟报标记 | `app/modules/replay/schedule.py` | `tests/test_replay.py::test_physical_order_beats_file_or_received_order` |
| TRAP-14 | `events.csv` 的 0088 指向两站 | 后写覆盖或重复扣车 | 同号异义的采信候选全部隔离 | `app/modules/intake/service.py` | `tests/test_intake.py::test_contradictory_duplicates_are_never_counted` |
| TRAP-18 | 空字节、空白行、仅表头上传 | 三种状态均报成功 0 行 | 返回各自的原因码 | `app/modules/intake/reader.py` | `tests/test_intake.py::test_three_empty_inputs_have_distinct_errors` |
| TRAP-19 | `plan.json` 卸车与 0008 借车同秒 | 输入顺序影响成交 | 明确容量、卸车、装车、尝试四级顺序 | `app/modules/replay/schedule.py` | `tests/test_replay.py::test_same_clock_arrival_before_attempt` |
| TRAP-23 | `events.csv` 的 0009 正好处于 08:15 | 同一笔计进两个槽位 | 时槽按左闭右开分配 | `app/modules/demand/buckets.py` | `tests/test_demand.py::test_boundary_bucketing_does_not_double_count` |
| TRAP-25 | `capacity.csv` 002 的同修订容量相互矛盾 | 落点随文件顺序变化 | 隔离同修订冲突，从剩余候选选最高版本 | `app/modules/capacity/versions.py` | `tests/test_capacity.py::test_conflicting_revisions_are_quarantined_together` |
| TRAP-26 | 样例班次更新后旧页面再发布 | 覆盖当前计划 | 事务内校验当前编号并返回 409 | `app/store.py` | `tests/test_archive.py::test_stale_publish_rejected_and_archive_can_be_restored` |
| TRAP-27 | 暴雨后整趟先结，返程车被同时算进目的地 | 四辆未到站车虚增目的地库存 | 现场凭证按 delivered/returned 拆分实际执行段，返程车在装车站 `return_unload` 回桩 | `app/modules/settlement/replay.py` | `tests/test_settlement.py::test_settlement_uses_actual_execution_and_returns_bikes_to_origin` |
| TRAP-28 | 同一现场凭证网络重试 | 重复结算、多出结算版本 | 结算编号由班次、规范化凭证和规则版本内容寻址，同输入同编号并幂等返回 | `app/modules/settlement/archive.py` `app/store.py` | `tests/test_settlement.py::test_retry_same_vouchers_is_idempotent_and_never_double_settles` |
| TRAP-29 | 发布失败或修正凭证 | 旧版结论被覆盖、原凭证被删 | 单事务写入，失败整体回滚；新凭证出新版，旧结论与全部凭证留档可回切 | `app/store.py` | `tests/test_settlement.py::test_storage_failure_rolls_back_and_keeps_old_conclusion` |
| TRAP-30 | 凭证车辆/站点/辆数与派车单不符 | 静默改账或错站结算 | 逐张比对原派车单，不符按原因码拒绝；返程凭证站点必须是装车站 | `app/modules/settlement/vouchers.py` | `tests/test_settlement.py::test_voucher_against_waybill_mismatches_are_rejected` |

## 对账规则

- 收件数 = 采信数 + 隔离数，空行和重现表头计入隔离。容量表按输入版本记录数勾稽，生效区间可由一条记录产生多段。
- 成交数 + 未成交数 = 有效尝试数，且事件编号逐一对齐。
- 站点期末 = 期初 + 成功还车 + 调入 − 成功借车 − 调出。每个动作后的全网车辆总量等于各站桩上、暂存、在骑、调拨在途之和。
- 补结按实际执行段回放：到站签收（delivered）记装车加目的地卸车；管制返程（returned）记装车、空驶返程和装车站回桩（`return_unload`），不进入目的地。失败的租还尝试库存不变，不计入站内流量。结算期末在途必须为零，全网车辆仍守恒。
