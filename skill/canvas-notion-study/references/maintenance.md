# 长期维护与恢复

沿用已登记实例，保留原配置、状态与 Notion 页面 ID。本文命令以现有 CLI 为准；使用 Notion 时由宿主连接器写入；未选用的工具跳过对应步骤。不要为“重新开始一次对话”新建实例或重复搭建数据库。

## 命令准备

先按用户指定学校／学期读取实例登记：默认是 `~/.codex/canvas-notion-study/instances.json`，设置 CODEX_HOME 时位于其 `canvas-notion-study/instances.json`。登记只保存非凭证路径；若有多个实例，按学校、学期和已验证 owner_id 选择。

将以下示例路径替换成实际绝对路径。优先使用宿主捆绑 Python；`study_state` 必须是配置解析后的 state_dir。

```sh
study_python="/absolute/path/to/python3"
study_script="/absolute/path/to/canvas-notion-study/scripts/study.py"
study_config="/absolute/path/to/instance/config.json"
study_state="/absolute/path/to/instance/state"
```

下文的 SOURCE_KEY、SESSION_ID、日期和文件名均替换为本次输出／用户输入。时间使用用户实际反馈和当前实例时区。

## 新会话开始

```sh
"$study_python" "$study_script" --config "$study_config" status
"$study_python" "$study_script" --config "$study_config" context
```

先读 `context.json` 的 profile、本周、优先任务、最近反馈、freshness、总数和 truncated。它只是重点摘录，完整信息在 complete_index 指定的文件中；不要把显示 40 项误当成总共 40 项。

需要一项的完整要求与材料时：

```sh
"$study_python" "$study_script" --config "$study_config" task --key 'SOURCE_KEY'
```

发现 Canvas 来源较旧／部分入口不可用，或 Notion 个人进度未读回时，先报告具体缺口并补相应数据。不要因新对话就整箱重搜邮件、重下载所有文件或重复 `study-start`；后者会新增真实学习会话记录。

## 例行来源更新

用户只要求整理既有周任务时，跳过本节采集与重新规划，运行 `week-render`（可用 `--week` 选择已保存周），再按 Notion 执行流程只应用 Weeks 的差异。先保存原计划、learner、同步映射与远端正文；读回确认任务链接、个人区和原页面 ID 保留。仅改版不需要重新运行 Canvas、Gmail、plan 或 week-plan。

1. 本次需要更新 Canvas 时运行同步和审阅包，检查每个入口的 coverage 与差异。

```sh
"$study_python" "$study_script" --config "$study_config" sync
"$study_python" "$study_script" --config "$study_config" review-packet
```

宿主阅读新增／变化的来源，更新 `enrichments.json`、`canvas-requirements.json`、必要的 `study-preparations.json` 和 `course-schedule.json`。课表输入及准备绑定见 [按课节学习安排](class-planning.md)。核对本人班别、停课、调课、隔周、推荐阅读及练习；材料发布后更新同一课节，独立提交任务继续保留。沿用要求 ID、任务 ID、步骤 ID；不要以“标题相近”合并不同交付物。未变的有效审核可复用，source_hash 改变必须实际重读。

2. 需要学业邮件更新时，生成有边界的查询，由宿主分页搜索并读取候选正文；保存消息文件和完整搜索 manifest 后再导入。

```sh
"$study_python" "$study_script" --config "$study_config" gmail-queries
"$study_python" "$study_script" --config "$study_config" gmail-import "$study_state/gmail-incoming" --manifest "$study_state/gmail-search-manifest.json"
"$study_python" "$study_script" --config "$study_config" gmail-review-packet
```

`gmail-incoming` 只放本批消息 JSON，不混入 manifest 或报告。宿主根据完整正文维护 `gmail-reviews.json`，记录分类依据、来源 hash、动作 ID、重复提醒的 canonical_task_key 和完成证据。部分查询失败或 pending_full 未清不能声称邮件范围已完整。

3. 重建来源任务与要求索引，读取个人进度后排周。以下 `import-personal` 仅在使用 Notion 时执行，其他情况沿用本地进度。

```sh
"$study_python" "$study_script" --config "$study_config" plan
"$study_python" "$study_script" --config "$study_config" import-personal "$study_state/notion-personal-rows.json"
"$study_python" "$study_script" --config "$study_config" week-plan
```

`notion-personal-rows.json` 来自宿主对已绑定数据源的实际完整读回，不能由本地旧值伪造。检查 personal-import-report 的冲突及 read_complete／applied_complete。`plan` 会重建 `requirements.json`，应检查来源覆盖和未匹配要求，而不是手动覆盖这个派生文件。

未提供可用时间时，`week-plan` 输出完整草案。用户提供当周空闲时间后再保存可用时间文件并执行：

```sh
"$study_python" "$study_script" --config "$study_config" week-plan --week YYYY-MM-DD --availability "$study_state/availability-current-week.json"
```

`plan --week` 和 `week-plan --week` 接受该周任一日期并归一到周一，默认当前周；week-plan 会按相同周基准刷新建议。时间文件可为非负分钟数、`{"weekly_minutes":300}`，或 `{"slots":[{"start":"实际 ISO 时间","end":"实际 ISO 时间"}]}`。只知道总分钟数就只分配分钟，不生成钟点。有时段时先扣除已确认课堂，再使用剩余容量的最多 80%；课前任务受 Prepare Before 限制，无法课前完成时明确提示并保留事项。

4. 使用 Notion 时检查并应用数据库的增量结构与 outbox。具体依赖、接管原周页和回执格式见 [Notion 执行](notion-apply.md)。每批完成远端写入与 fetch 后再提交 receipts。

```sh
"$study_python" "$study_script" --config "$study_config" notion-next --kind tasks --begin --out "$study_state/batch-tasks.json"
# 宿主应用该批次，并保存经 fetch 核验的逐项 receipts。
"$study_python" "$study_script" --config "$study_config" notion-receipts "$study_state/receipts-tasks.json"
```

对本次变化涉及的其他 kind 按依赖顺序重复。Weeks 和 Sessions 必须等关联的 Task／Week 页面已绑定后再写；不要直接发送未解析的 `[[record:...]]` 占位符。

5. 核对并生成本地阅读入口及恢复包。使用 Notion 时读回本次涉及的记录，再执行下列前两条命令；本地 export 与 context 按需要运行。

```sh
"$study_python" "$study_script" --config "$study_config" audit --notion-rows "$study_state/notion-all-rows.json" --out "$study_state/audit.json"
"$study_python" "$study_script" --config "$study_config" notion-next --out "$study_state/repeat-check.json"
"$study_python" "$study_script" --config "$study_config" export
"$study_python" "$study_script" --config "$study_config" context
```

相同已应用计划的 repeat-check 应为零 operations、零 blocked。`audit --baseline 文件.json` 可额外比较文件审计基线；当前文件全部 hash 匹配也不能代替独立来源覆盖核验。未解锁或无权限文件保留准确状态，不用重复请求掩盖缺口。

## 一次学习与反馈

使用 Notion 时，推荐前先读回 Done、Planned、Priority、Personal Notes，再 import-personal；同一次对话刚核对且没有后续修改时可复用，避免忽略学生在 Notion 中更新的进度。真正开始学习时指定可用分钟数；需要主动选择某项时加 `--task 'SOURCE_KEY'`。

```sh
"$study_python" "$study_script" --config "$study_config" study-start --minutes 45
```

保存返回的 session ID，依据理由、材料和完成标准学习。默认建议优先课程学习，行政／状态核实在 `verification_reminders` 中保留；若需要先办理某项，用明确的 `--task` 选择。无具体办理步骤或估时的核实项默认 5 分钟只用于确认状态与下一步，不表示能办完。

结束后将用户实际反馈写入文件；例如以下字段都必须来自用户报告，不预先假定完成：

```json
{
  "spent_minutes": 35,
  "difficulty": "hard",
  "step_completed": false,
  "remaining_work": "仍需核对第二题的推导",
  "remaining_minutes": 30,
  "task_completed": false
}
```

```sh
"$study_python" "$study_script" --config "$study_config" study-finish --session 'SESSION_ID' --feedback "$study_state/session-feedback.json"
"$study_python" "$study_script" --config "$study_config" context
```

session 和进度立即写入本地 learner；使用 Notion 时再按前述 outbox 流程更新。实际任务完成可以由宿主明确更新 Done 后读回；时长、会话结束或步骤完成都不自动证明知识掌握。保存反馈后，下一次建议使用剩余工作和困难；需要更新整周安排时再运行 week-plan，不删除旧 session。

## 状态格式与备份恢复

本地学习状态使用 schema 2；Canvas、投影和附件等数据按各自的 schema 保存。

需要调整状态格式时，先备份实例并核对待决操作，再预览迁移：

```sh
"$study_python" "$study_script" --config "$study_config" migrate-state
```

默认只报告 changes，不写文件。核对路径与变更后应用：

```sh
"$study_python" "$study_script" --config "$study_config" migrate-state --apply
"$study_python" "$study_script" --config "$study_config" context
```

迁移预检 `notion-state.json`、`learner.json` 和 `state-schema.json`；未知更高版本在任何写入之前拒绝。应用前在 `migration-backups/时间戳-标识/` 保存原文件及含 SHA256 的 manifest，保留原页面映射、个人字段、history、inflight 和未知业务字段。缺失 learner／版本标志会创建，不会重建 Notion 八库，也不会批量修改源快照格式。

这份备份覆盖迁移涉及的状态文件，不等于整个学期归档备份。长期保留 config、状态目录、来源归档及处理记录。路径改变时，修正配置路径后登记同一实例：

```sh
"$study_python" "$study_script" --config "$study_config" register
```

register 需要此前 probe 保存的 owner_id。不要为了更新登记重新生成身份或新的 Notion 根页。

## 中断、冲突与恢复

| 情况 | 恢复动作 |
| --- | --- |
| context 缺失或陈旧 | 从现有 plan、learner、snapshot 重新运行 context；它是派生文件，不重置档案 |
| 新一周没有计划 | 读回个人进度后生成该周 draft；不自动复用上周空闲时段 |
| 审核失效阻止 notion-next | 重读对应 evidence packet，更新有效审核，再 plan；不删除警告或只改 hash |
| inflight 存在／调用超时 | 查询原绑定数据源核实 Source Key，检查实际内容后提交真实 receipt；不要盲目重建 |
| 页面管理区与基线不同 | 保留原 fetch 和差异，判断用户修改范围；不整页覆盖或直接改成本地目标基线 |
| 本地映射丢失 | 优先恢复备份，并逐库按 Source Key 与实际 page ID 对账；没有完成对账前不运行 create |
| 部分 Canvas／Gmail 读取失败 | 保留成功结果与旧原件，标明 stale／unavailable 或未完成游标；补读对应缺口 |
| 想退回旧软件／状态 | 保留当前副本，先比对迁移 manifest 与迁移后的真实远端变化；没有自动 rollback CLI，不能用旧映射覆盖已发生的远端写入 |

同一实例的写入由一个宿主串行推进，避免同时更新 learner 或 outbox。恢复结束再次完整对账并保留报告，再把“完成”作为有证据的结论。
