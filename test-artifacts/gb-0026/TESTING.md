# 来源资料包往返与三方合并 — 测试记录

## 环境

- Python 3.11.2 + FastAPI 0.115.6 / SQLAlchemy 2.0.36 / scikit-learn 1.6.1
- 测试用临时 SQLite（`DATABASE_URL=sqlite:///…`），队列替换为内存假 Redis；HTTP 经 Starlette `TestClient` 真实走路由、鉴权、校验与响应模型。
- 运行：`PYTHONPATH=/tmp/pylibs:/workspace/backend python3 -m pytest -q test_merge.py`
- 前端：`npm ci && npx tsc --noEmit && npm run build`。

## 覆盖场景

| 测试 | 验证点 |
|---|---|
| `test_round_trip_is_identity` | 导出→原样回收为 no-op（`ignored`），再次导出记录与基线完全一致 |
| `test_values_split_and_notes_merge_without_conflict` | 数值+批注同窗修改合入，`last_branch` 记录来源分库 |
| `test_replay_does_not_undo_merged_edits` | 两分库从同一基线各自修改；后回收的旧包为 replay，不撤销已合入值 |
| `test_out_of_order_two_branches_both_one_pass` | 乱序回收两分支都一次通过，重复回收为 replay |
| `test_conflict_blocks_whole_package_and_resolution_is_atomic` | 同字段异改→整包 conflict，包内无冲突字段也不落地；创建研究 409；选“保留本库”后值不变、重复裁决 409 |
| `test_theirs_resolution_applies_whole_package_atomically` | 选“采用该分库”整包原子生效（冲突字段+无冲突字段一起） |
| `test_modified_after_conflict_requires_reimport` | 待裁决期间本库再改→旧裁决点失效 409，按新基线重新导出后合入 |
| `test_identity_not_row_number` | 匹配依据是 `origin_uid`，不是数据库行号 |
| `test_viewer_cannot_import` | 阅读者导出/回收均 403 |
| `test_baseline_history_is_traceable` | 基线修订单调、可排序追溯 |
| `test_frozen_run_and_export_survive_merge` | 合并后既有冻结运行的 `input_snapshot` 不变 |
| `test_run_input_hash_tracks_merged_data_only` | 新运行快照含合并后数值，训练计数不变 |
| `test_same_data_different_bookkeeping_has_same_input_hash` | 修订/分支簿记字段不进入 input_hash；真实数据改动才改变哈希 |
| `test_concurrent_resolution_only_one_wins` | 两线程同时裁决同一包：一个 200、一个 409，值只落一次 |
| `test_concurrent_import_same_package_is_idempotent` | 两线程同时回收同一包：两次 200，恰好一次真实写入，无重复行 |
| `test_partial_value_edit_keeps_other_features` | 只改一量时其余量保留；编辑后可原样往返 |
| `test_note_can_be_cleared_and_counts_as_edit` | 批注可清空并作为一次编辑进入资料包 |
| `test_tampered_package_rejected` | 内容指纹不匹配 422 |

结果：**18 passed**。前端 `tsc --noEmit` 与 `vite build` 通过。

## 已知边界

- 单进程内用研究级互斥锁；PostgreSQL 上另有 `SELECT … FOR UPDATE` 行锁与条件 UPDATE 保证跨 worker 只有一个裁决成功。
- 待裁决期间整包挂起，不提供逐字段手工合并（只提供整包选边）。
- 资料包要求两端四量定义（code/position）一致，且窗口集与所声明基线一致；结构性差异（增删窗口、改特征）明确拒绝而不是静默合并。
