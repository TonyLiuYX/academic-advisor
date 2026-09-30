# Notion 执行与已有页面接管

本文负责远端结构、写入和核验；日常命令见 [长期维护与恢复](maintenance.md)，数据含义见 [导师工作流](workflow.md)。CLI 生成本地计划和 outbox，实际 Notion 操作由宿主的已连接工具执行。

## 八个数据库及写入顺序

先用连接器 `fetch("self")` 核对工作区，并读取 `notion://docs/enhanced-markdown-spec`；操作视图前读取 `notion://docs/view-dsl-spec`。以当前工具 schema 与数据源实际字段为准，不猜标题列、库 ID 或可用命令。

| kind | 显示名称／用途 | 主要关系 |
| --- | --- | --- |
| `courses` | Courses：正式课程 | — |
| `resources` | Resources：原件、页面、外部资料入口 | Course → courses |
| `tasks` | 已有 Assignments／Tasks：作业、行动项、课前准备 | Course → courses；Resources → resources |
| `announcements` | Announcements：通知和邮件信息 | Course → courses |
| `notes` | Notes：课程规则与个人笔记入口 | Course → courses |
| `timetable` | Timetable：有来源的日程 | Course → courses |
| `weeks` | Weeks：周计划 | Tasks → tasks |
| `sessions` | Study Sessions：学习记录 | Task → tasks；Week → weeks |

复用已绑定数据库，只创建当前流程缺少的数据库。依赖顺序为 `courses → resources → tasks → announcements/notes/timetable → weeks → sessions`。`schema --kind KIND` 输出建库定义；已有库先 fetch，再把解析出的 data-source-state JSON 交给 `schema --kind KIND --fetched-schema schema.json`，生成新增字段、关系或枚举的增量语句。应用后重新 fetch，保留原选项、颜色和个人列。数据库容器 ID 与 `data_source_id` 不同；用 `bind --kind KIND --database-id ID --data-source-id ID` 保存两者。

Tasks 中 `Due` 是官方截止，`Study Date` 是建议学习日期，`Planned` 是个人计划；不能用一个字段替代另一个。Tasks 还保存 Planning Origin、Learning Activity、Preparation Status、Class / Assessment 和 Prepare Before。助手建议不计入官方要求账本；教师推荐可以进入学习安排，已取消／合并建议退出活动视图。`Scope` 与 `Evidence Status` 保留原语义。不要因新增字段重建任务或更换 Source Key。

## 先读回个人字段

用已绑定 Tasks 数据源的 faithful rows 或已保存 view 分页读取，保存原始工具响应；不要用可能丢失富文本的 SQL 结果修复个人笔记。需要分页时，按当前工具返回的游标继续，合并所有页后才声明完整。其他数据库中的 Personal Notes 也可按相同格式导入。

执行 `import-personal rows.json` 合并 Done、Planned、Priority、Personal Notes；支持 Notion 工具 wrapper、扁平日期和 Source Key 自动链接。缺页或缺字段不会清空旧值，明确 `false`、空值和空字符串才代表相应更新。检查 `read_complete` 与 `applied_complete`：末页已读完不等于所有行都已成功匹配；身份冲突、未解析行或旧版本读回仍需处理。没有 `has_more=false` 的数据不能作为完整读回证明。

个人正文继续留在 Notion 页面，`import-personal` 不把全文导入 learner。普通同步省略所有个人字段。用户明确报告 `task_completed` 时，`study-finish` 只更新本地个人状态；如需同步勾选，由宿主显式写入对应 Done 并再次读回，不能把个人字段混进来源 outbox。

## 接管已有周计划页面，保留 URL

已有按日说明的周计划页面应进入 Weeks 数据源，不能为同一周再建副本。此步骤使用宿主工具和受控本地映射迁移，当前没有 `adopt-week` CLI 命令。

1. fetch 旧页，保存页面 ID、URL、标题、完整正文、个人区及 linked views；查询 Weeks，确认没有另一条相同周 Source Key。
2. 缺少 Weeks 时创建并绑定；调用当前工具支持的 `move_pages`，目标使用 `new_parent.data_source_id`。移动后 fetch，确认仍为原 page ID 和 URL，原有正文、子页面和视图都在。
3. 将原标题保存到 `learner.json` 的 `weeks[周一日期].title`。`week-plan` 会沿用它，并产生该周稳定 Source Key。只补充该记录所需属性，不复制旧正文。
4. 旧页还没有管理区时，在既有内容之外追加唯一的一对“课程资料同步区／个人补充区”标记；用 `insert_content` 的 `content` 字段，勿用 `replace_content` 重写整页。已有标记先核对范围，不追加第二对。
5. 根据实际 fetch 建立该周 Source Key → 原 page ID 的映射和 `remote_region` 基线；保留迁移前副本。不要把尚未写入的目标计划 fingerprint 登记为已应用。之后 `notion-next` 应生成原页 update；若仍为 create，先修复映射。

旧的人工按日说明与 linked views 留在管理区外；管理区内展示当前周计划。正文中的 `[[record:SOURCE_KEY]]` 由 outbox 解析成已绑定页面的 Notion mention。源任务或周尚未绑定时会阻塞，不能把占位符原样发送，也不要将其替换成裸长 ID。

## 每批写入与回执

课堂长笔记带 `content_mode=remote_append`，仍属于 Notes，但普通 `notion-next` 不替换正文。出现 `use_notes_next` 时转到[Notion 课堂笔记](class-notes-notion.md)，使用 `notes-next --begin`／`notes-receipts` 完成原稿上传和逐批读回，不用普通回执绕过附件核验。

已存在的课堂笔记若课程属性变化，普通队列返回 `action=update_properties`：只将 properties 写到 page_id 并读回属性，不调用 content-edit，不发送空正文；使用普通 notion-receipts 记录这次属性更新。课堂正文与原稿仍由 notes 队列管理。

执行 `notion-next --kind KIND --begin --out batch.json`，在远端调用前持久化 inflight。没有 `--begin` 仅预览。检查 blocked；审核尚未完成或 source_hash 失效时，CLI 会阻止写入，先阅读新证据并重建 plan。

- create：使用 batch 的 data_source_id、properties 和 content。日期为 `date:字段:start`／`date:字段:is_datetime`（数值 0/1），checkbox 为 `__YES__`／`__NO__`，关系为已解析的实际 page ID。
- batch 的 properties 已为 Local Path 做 Notion 字面文本转义，不要再次转义。若存在 source_properties，它保留原始属性值；回读时将 Notion 包装解码后与原值比较。receipt 会保存原始路径，文件系统、版本归档和后续计划不会混入 Markdown 反斜杠。
- update：先 fetch 当前页，保存工具响应中的页面 Markdown 为文本文件。`content-edit --source-key KEY --fetched-content page.md` 基于 inflight 与实际基线生成定点替换；不要把 JSON wrapper 当作 page.md。属性更新和正文更新均成功后再提交回执。
- 长周页的定点替换若被远端以“未匹配”拒绝，先核对最新 fetch 与已保存基线。正文未变化且本次明确授权重排同步区时，可回写完整正文，但必须逐字保留同步区外内容及原子页、数据库标签；不要设置允许删除子内容。回读核对这些内容及原页面 ID 后再记回执。
- 异步／结果不明：等待真实完成，或按 Source Key 在绑定源中对账。唯一结果且内容匹配才能成功记账；多条结果是重复冲突；仅在确认未执行时使用 `definitely_not_applied`。不要删除 inflight 后盲目重试。

每条成功结果必须单独映射，不能把“批次请求成功”当作所有记录完成。成功后再次 fetch，取两标记之间实际返回的 Markdown 作为 `remote_region`；Notion 会规范化空行、缩进和链接，不能直接拿请求正文充当基线。

```json
{
  "receipts": [
    {
      "source_key": "来自 batch 的原值",
      "operation_id": "来自 batch 的原值",
      "status": "succeeded",
      "page_id": "已验证的页面 ID",
      "remote_region": "fetch 后两个管理标记之间的实际文本，不含标记"
    }
  ]
}
```

用 `notion-receipts receipts.json` 提交。管理区被用户改动、基线缺失或标记不唯一时，保存冲突并核对；不能整页覆盖。源字段消失时，outbox 会显式清空旧来源值，而非仅省略它；个人值始终保留。

## 视图与最终核验

`views` 输出根页和课程页的 linked-view 参数。已有 `existing_view_id` 时读取并更新原视图；未记录 ID 也先检查父页，避免重复创建。使用返回的 `view://...` 读取／查询视图，裸 ID 可能被解释成页面。新视图验证后用 `bind --view-key KEY --view-id ID` 保存。

周页采用[按日、按课节版式](class-planning.md#周计划版式)：每周一至周日独立成组，主周展开，后续两周各自折叠，周日后明确分隔。往周未完成、无日期待核实、远期及历史记录分别收起。任务引用应可点击，原生引用前不重复完整标题；所有事项仍完整保留。个人档案是页面／本地 profile，不是第九个业务数据库。

完成后完整读回本次涉及的数据源，合并记录并运行 `audit --notion-rows all-rows.json`。若只查一个库，应以相同范围审计，不能把范围不足当成其他库丢页。核对 Source Key 唯一性、原 page ID、关系、日期、个人字段和代表性正文，再重复 `notion-next`：相同已应用计划应为零 operations、零 blocked。保存原始查询、fetch、batch、receipts 和报告供恢复。
