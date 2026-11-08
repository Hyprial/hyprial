# scripts/

仓库自用脚本，供操作者（人或 agent）在主机上运行——不被产品包 import。
2026-10-02 起按用途分桶（重构方案 §5.2）：

- `ci/` — CI 支撑：范围分类（ci-scope / ci-plan）、步骤封装（ci-step.sh、ci-conditions.sh）、
  工具链就位（ci-ensure-*.sh）、pytest 影响面选择（pytest_impact.py + pytest-impact-map.json）、
  公网发布门（github_publish.py）、文档判定（docs_only.py）。
- `build/` — 构建：`build-wheel.sh`（发布 wheel，PEP 440 本地段 `X.Y.Z+g<short7>` 标记精确
  HEAD commit；印记用临时 `uv version` 写入并总是还原；pyproject/uv.lock 有未提交改动时拒绝运行。
  仓库政策：`pyproject.toml` 只携带手工声明的 `X.Y.Z`（`.devN` 已退役），本地段只存在于
  发往 Forgejo PyPI registry 的构建产物）、`docker/`（worker 镜像三件套）。GUI 随产品 wheel
  配对打包；旧独立 catalog release builder 已退役，不保留第二分发权威。
- `verify/` — 验证仪器：验收/落地脚本组（devcheck/intggate/onbranch/premerge/postmerge/
  tagaccept/vlib，原 tools/verify）、发布与安装核验（verify_*）、契约与残留检查
  （check_contract_impact、check_cli_help_inventory、check_hyprial_merge_residuals）、
  探针（channel-probe.py）、`zenoh-capability/`（P1 能力闸门，原 spikes/zenoh）。
- `archived/` — 一次性脚本留档（不再维护，仅供追溯；`cleanup_resolved_retry_dlq.py` 为
  pull-retirement 缺陷留下的误报 DLQ 行的精确 ID 修复工具：需要 inbox 行与收件方
  `fetched/RECIPIENT_COMMIT` 双重证据，默认 dry-run，apply 模式需显式确认并先做
  mode-0600 SQLite 备份，仅删除选中的 DLQ 行）。
- `refactor/` — 深模块重构的 C0 扫描与迁移脚本套件（进行中，见
  `docs/design/refactor-c0-and-tooling-2026-10-01.md`）。
