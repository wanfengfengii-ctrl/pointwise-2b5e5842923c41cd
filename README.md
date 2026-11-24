# 水质检测中心日批数仓

现场采样员提交设备报送，值班员验料、签发或退役采样口，夜班核账员用日账和原行完成交班。项目使用 Python 3.11 标准库和本地 SQLite。所有命令从项目根目录运行。

```text
python3 -B check_project.py
python3 -B demo.py
```

`check_project.py` 是控制台提供的完整回归入口，覆盖 `tests` 包中的全部 `test_*.py`。演示默认使用独立临时目录，也可用 `python3 -B demo.py --workspace ./demo-run` 指定一个空目录。演示会拒绝非空目录，样例来自 `sample.json`。混合批的 1 升、5 毫升和一条缺测产生 3 行、1005 毫升。再签发 7 毫升后，夜班凭两份回执领取 4 条原行，复算得到 1012 毫升。演示同时核验退役先提交时的 5 毫升和接纳先提交时重开可见的 12 毫升。

夜班演示（`v9`）另用同一口的三份正式回执办理分装：两份回执各有一条 10 毫升且同一行号的原行，分装只扣被点名的那一份；100 毫升原行先分入 R1、R2 两瓶，再各取 30、20 毫升合成 C，两份按各自瓶路径分别记账。一次超量合瓶请求整件退回，`NEVER` 瓶未签发、余额不变。断开目录重开后按瓶号仍核出 C 的各份出处与余量，并从 C 的第二份再分出 D。证据演示（`v10`）改动归档原值后，按瓶号给出 `archive_integrity`、具体瓶份及受阻原因。

## 一天的交班

1. 值班员登记采样口与设备的固定绑定，供采样员确定报送来源。
2. 采样员按业务日提交请求号和有序原行。验料检查设备、原行号、原值、单位和缺测表示，保存合格的完整批次。
3. 值班员用请求号和暂存批次号签发。成功结果包含正式位置和回执，全部事实及归档随之可用。
4. 午班以具名请求退役采样口。成功回执给出实际生效位置。
5. 值班员继续处理待签报送或重投丢失回包的请求。退役前已获准的报送可恢复原结果，退役后首次签发失格。
6. 核账员按口日获取行数、总毫升以及按接纳位置排列的回执。
7. 夜班保存同一日账视图，关闭目录连接，凭回执领取各批的固定原行。
8. 夜班按原单位复算，逐批核对出处和毫升值，再比对全日行数及体积。
9. 夜班从已签发原行分装检测瓶，或把几个已封瓶的部分汇成合样；断开目录后仍按瓶号核账。

## Python 接口

`catalog.registry.open_catalog(path)` 和 `publication.storage.open_warehouse(path)` 返回调用方负责关闭的连接。分别用 `initialize_catalog(catalog)` 和 `initialize_warehouse(warehouse)` 初始化。两项初始化都可安全重复运行，保留已登记绑定、退役及事实。一个仓库使用一对固定的数据库文件。并发调用需使用各自连接，事务型入口负责其自身事务。

| 入口 | 参数与成功结果 |
| --- | --- |
| `catalog.registry.register` | `(catalog, point_id, device_id)` 返回登记及 `active` 状态。采样口及设备均只能登记一次。 |
| `intake.staging.stage` | `(catalog, warehouse, request_id, point_id, business_day, rows)` 返回 `StagedBatch`，含 `batch_id`、验料行数、总毫升和有序已验料行。 |
| `publication.admission.seal` | `(catalog, warehouse, request_id, batch_id)` 返回 `AdmissionReceipt`，含 `receipt_id`、正式位置及原批身份。 |
| `catalog.retirement.retire` | `(catalog, warehouse, request_id, point_id)` 返回 `RetirementDecision`，含原请求、采样口、正式位置和 `receipt_id`。同号同口重投返回原终局。 |
| `account.day.read_day` | `(catalog, warehouse, point_id, business_day)` 返回 `DayAccount`。有效空日返回零行、零毫升及空回执。 |
| `archive.retrieve.claim` | `(warehouse, receipt_id)` 返回 `ClaimedBatch`，含固定采样口、业务日和按输入顺序排列的 `rows`。这条入口可在目录离线时使用。 |

`intake.rows.SourceRow(device_id, line_number, value, unit, missing=False)` 的 `line_number` 为正整数，批内顺序由输入位置确定。单位用 `L` 或 `mL`。明确缺测用 `value=None` 和 `missing=True`，仍保留原单位。整数升乘 1000，毫升取原值，缺测计一行且不增加体积。编号和数值使用精确整数表示，业务日期用 `YYYY-MM-DD`。验料要求非空批次。请求比较保留完整原值和原单位，等量改写也会冲突。

`account.day.read_day_snapshot(catalog, warehouse, point_id, business_day)` 取得日账和同一视图下的逐批证据。`account.reconcile.reconcile_day(warehouse, snapshot)` 仅使用这份视图及固定归档，返回差异元组。差异包含原因、可定位的回执及原行号，以及适用的预期与实值。空元组表示核对一致。

登记冲突分为 `PointRegistrationConflict` 和 `DeviceBindingConflict`。未知口报 `UnknownPoint`，未知设备报 `UnknownDevice`，来源错配或非法行报 `InvalidSourceRow`，无效量值报 `InvalidReading`，无效日期报 `InvalidBusinessDay`。同号改投报 `StageRequestConflict`、`AdmissionBindingConflict` 或 `RetirementRequestConflict`。缺暂存报 `UnknownStagedBatch`，首次签发退役口报 `RetiredPoint`，另用新号退役已退役口报 `PointAlreadyRetired`。未知回执报 `UnknownArchiveReceipt`，原批不完整报 `ArchiveIntegrityError`。SQLite 写入或提交失败保留原有正式结果，并将数据库错误返回给调用方。回执是本仓业务标识，使用者需已具备仓库访问权限。

## 分样合瓶账

分装与合瓶只写入仓库数据库，不依赖目录。`initialize_warehouse` 会一并建立瓶账表。

| 入口 | 参数与成功结果 |
| --- | --- |
| `bottling.split` | `(warehouse, request_id, bottles)` 返回 `SplitDecision`，含 `receipt_id`、`position` 及按请求顺序排列的 `IssuedBottle`。一次可同时扣减多瓶、多原行并签出多个新瓶。 |
| `bottling.read_bottle` | `(warehouse, bottle_id)` 返回 `BottleAccount`，含首次签发组成、各来源份、可用余量、逐段转手和证据问题。目录离线可用。 |
| `bottling.read_original_row` | `(warehouse, receipt_id, device_id, line_number, row_ordinal=None)` 返回 `RowAccount`，含该原行的毫升、已领出量及全部首次分装出处。 |

来源只有两类。`bottling.AdmittedRowSource(receipt_id, device_id, line_number, milliliters, row_ordinal=None)` 从某正式回执的准确原行领用；同一回执出现重复设备与行号时必须给出 `row_ordinal`。`bottling.BottlePortionSource(bottle_id, portion_ordinal, milliliters)` 从已封瓶的某个来源份领用。`bottling.NewBottle(bottle_id, sources)` 列出该新瓶每一来源份及其毫升数；同一来源只可在一个新瓶中出现一次。

初次分装只认正式回执、原行与毫升数。库存按原行身份分别记账：另一回执或另一原行的等量行不能替代已绑定来源；缺测行和零量行不能分装。再分装或合瓶可跨一个或多个已封瓶领用；含多种原样的瓶必须逐份给出毫升。相同原样经不同瓶进入同一合瓶时，各份按不同来源瓶与路径分别记录，不会并成一份。

来源、回执或余量任一不成立，整件退回，所有原瓶余额与既有瓶不变。同一瓶并发办理只使用提交时的实际剩余量。同号同参重投返回首次 `SplitDecision`，异参报 `SplitRequestConflict`。来源批后续退役或下游继续分装，不改变原瓶签发时的组成与各份毫升。

断开目录、关闭连接并重开仓库后，`read_bottle` 仍按瓶号给出首次组成、可用余量和逐段转手；`read_original_row` 给出该原行的全部领用出处。证据不符时 `BottleAccount.issues` 逐项给出 `reason`、具体 `source`（瓶、份、回执或原行）、预期与实值；`split` 会抛出 `EvidenceMismatch` 且不扣量。

结构不完整报 `InvalidSplitRequest`；瓶号已占用报 `BottleAlreadyExists`；来源不足、缺测、零量或非正数报 `SourceUnavailable`，带 `reason` 与预期/实值；未知瓶或份分别报 `UnknownBottle`、`UnknownBottlePortion`；无法唯一对应原行报 `UnknownOriginalRow`。

