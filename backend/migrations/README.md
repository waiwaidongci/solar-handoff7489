初始结构由 `src/models/tables.py` 定义，在 API 启动前由 `src/models/migrate.py` 的 `ensure_schema` 创建。演示数据通过独立入口填充。

升级到带网关校时治理的版本时，`ensure_schema` 会幂等补齐：
- `analysis_runs`：`calibration_version`、`calibration_basis`、`calibration_stale`（默认 0）；
- `report_schedules` / `report_deliveries`：`calibration_version`；
- 新表 `clock_calibrations`（只追加的批次校时记录，含跨数据版本的批次版本唯一约束）。

已存在的列或表会跳过，可重复执行；`calibration_stale` 只做 0→1，旧校时版本无法恢复待发报告的投递资格。
