# 学业规划工作流与持久上下文

以来源证据维护任务和学习记录，由宿主模型审阅并解释，本地 JSON 保存状态。按用户需要选用 Canvas、Gmail、Notion 和 Obsidian；下文只执行当前配置和任务涉及的步骤。实际操作命令见 [长期维护与恢复](maintenance.md)，远端写入见 [Notion 执行](notion-apply.md)。

## 证据、要求、任务与个人进度

一次来源更新依次完成：采集所选来源 → 宿主阅读来源 → 稳定要求清单 → `plan` 生成任务 → 读取已保存个人进度 → `week-plan`。使用 Notion 时再完成个人字段读回、outbox 写入与回执。随后 `study-start`／`study-finish` 保存学习过程，`context` 为下次对话生成恢复包。

`plan` 不替代来源阅读。Canvas 的 `enrichments.json` 和 Gmail 的 `gmail-reviews.json` 绑定 source_hash；来源变化后重新审阅，不只替换 hash。`canvas-requirements.json` 保存 Canvas 的直接要求审阅；`requirements.json` 是合并后的“来源 → 独立要求 → 最终任务”索引，由 plan 重建。多个提醒可指向一个任务，一封邮件也可包含多个要求。任务措辞变化沿用原 ID；缺日期的任务仍保留。没有经过独立来源审阅，单靠现有任务清单不能证明要求无遗漏。

| 信息 | 含义与处理 |
| --- | --- |
| Due | 官方截止；未知留空，冲突保留候选，不选一个猜测值 |
| Study Date | 建议学习日期，不写入 Due，也不虚构个人学习钟点 |
| Planning Origin | Teacher requirement 教师要求、Teacher recommendation 教师推荐、Assistant suggestion 助手建议 |
| Prepare Before | 已确认课节／考核的准备边界，与官方提交截止分别记录 |
| Planned | 学生个人计划；与 Due 冲突时提示并保留原值 |
| Done | 学生明确确认的完成状态；不会从学习时长推导 |
| Canvas Status | Canvas 提交／评分状态；与个人勾选、掌握程度分别保存 |
| Evidence Status | `confirmed_completed` 表示来源确认完整完成；`partial_completed` 只确认部分，剩余要求继续保留 |
| Scope | Academic 等必做范围、Optional 可选、Reference 参考、Historical 历史分开呈现 |

提交回执或培训部分通过通知只作用于明确匹配的 task_keys。`acknowledged`／`unknown` 不自动完成任务；部分完成即使伴随 Canvas submitted 也要保留来源指出的剩余工作。分组编号等无需交付的信息可作为 Reference 保留；“届时查看分组结果”若确为行动，应是另一个有来源的任务。

## 每节课的准备与每周计划

课程阅读、题目准备等存入 `study-preparations.json` 的 `preparations` 数组。每项至少有稳定 `id`、`course_key`、`title` 和 `source_refs`，并提供可解析的 `source_url` 或对应资源来源；可补 `study_date`、`resource_keys` 以及带稳定 `id` 的 `study_steps`。来源明确时用 `study_mode: assessment_preparation` 标注考核准备，`logistics` 标注携带物品等事务准备；未知不按标题猜。确为同一既有任务时使用 `canonical_task_key`，避免重复建立“阅读”与“作业准备”。具体章节、题号和产出标准必须来自已读材料；通用 prepare/work/check 步骤不能代替来源要求审阅。

`course-schedule.json` 保存本人确认的课节、有效日期、隔周和停课调课信息，以及考核和推荐练习。`plan --week` 与 `week-plan --week` 使用同一周基准，只生成本周及后两周的具体建议，保留远期官方事项。课节／考核身份不随标题或调课改变；已有准备通过绑定复用。规则见 [按课节学习安排](class-planning.md)。一般回顾错过后合并进同课程同系列下一次准备，原记录保留合并去向；官方要求不自动完成或取消。

可用时间由用户明确提供，可为分钟数、`{"weekly_minutes":300}`，或带起止时间的 `slots` 数组。重叠时段合并，过去时段不重算；已知容量最多安排 80%，余项明确保留。只有总分钟数时不生成虚构钟点；未知时间为完整 draft。某周的时段不能静默当成以后每周固定可用时间。

课表只使用本人已确认的班别、来源日程或明确提供的时间。`learner.profile.confirmed_sections` 记录已证实的组别与地点，`unknowns` 保留缺口；不能由课程目录、同学分组或课时数推断学生的 PRA／tutorial 安排。

周页按日期和课节展示本周准备，另保留后两周、无日期待核实和远期事项。未安排区汇总原因并保留完整清单。教师推荐阅读／练习即使不计分也纳入安排；其余 Optional 仅在用户设置 Priority／Planned、主动指定或选择 include_optional 时纳入。已取消、合并、过期或暂缓的建议退出活动视图；完成、可选行政、参考和历史另列。

## 持久存储与恢复边界

| 文件／目录 | 保存内容 |
| --- | --- |
| 实例 `config.json` | 学校、学期、时区、选课／Hub、Gmail 查询范围、路径、Notion 数据源与视图绑定 |
| `snapshot.json`、`snapshots/` | 当前及历次 Canvas 原始证据、入口状态、分页和发现 ID |
| `gmail-snapshot.json` | Gmail 稳定消息 ID、完整正文、附件索引、查询清单与完整搜索水位 |
| `enrichments.json`、`gmail-reviews.json` | 宿主审阅结果、分类依据和来源 hash |
| `canvas-requirements.json`、`study-preparations.json` | 来源直接要求与课前准备输入 |
| `course-schedule.json`、`schedule-state.json` | 已确认课表／考核／推荐输入；规则任务身份、状态、合并去向和课时边界 |
| `requirements.json`、`notion-plan.json` | 当前要求对账和八库目标记录；由输入重建 |
| `notion-state.json` | Source Key → page ID、实际正文基线、inflight 与回执历史 |
| `learner.json` | profile、个人字段读回、task_progress、各周计划、所有 sessions |
| `weeks/YYYY-MM-DD.json` | 对应周最近一次保存的完整计划；重排同周会更新该文件 |
| `record-dispositions.json` | 对已发布记录的显式历史／范围处置，保留其原 ID |
| `context.json` | 可重新生成的短恢复包，不是完整档案 |
| 归档目录、导出 manifest、reports | 原件版本、哈希、阅读入口与处理记录 |

凭证留在 Keychain 或当前进程环境；本地档案、原始邮件和课程内容不进入可分发 skill 包。长期记忆来自实例文件和已配置的保存位置，不依赖旧聊天仍在上下文中。

新对话先用 registry 找到原配置，再运行 `status` 与 `context`。恢复包包含已确认 profile、本周、优先未完任务、近期截止／日程、最近最多 5 次学习记录和新鲜度。未完显示最多 40 项，近期信息最多 25 项，周内各清单最多 40 项，同时给出真实总数、truncated 和完整文件路径。遗漏显示不等于任务删除；用 `task --key SOURCE_KEY` 读取单项及其资料、个人进度和剩余工作。

`study-start` 会创建并保存 session，只有真正开始学习时调用；查看近况使用 context。默认优先课程学习，把 Academic Admin／Needs confirmation 单列为 `verification_reminders`；没有学习项时才建议核实，主动指定行政任务仍可开始。同一临近日期优先明确标注的考核准备。没有具体步骤、估时或反馈剩余时间的未知行政核实项只默认预留 5 分钟检查状态、记录下一步，不表示能在 5 分钟办完；已知剩余办理工作与明确估时继续保留。

`study-finish` 接收实际分钟、困难、已完成步骤和剩余工作，只有明确 `task_completed` 才修改本地 Done。源任务、步骤完成和知识掌握始终分开。

## Canvas、Gmail 与覆盖要求

正式课程检查作业、通知双入口、可读回复、Modules、主页、Pages 和 Files。列表按页读取；目录受限时继续读取模块／页面中的明确附件和同课页面链接。Hub 读取通知、任务及其关联附件，默认不遍历全部历史文件。`coverage` 区分 fresh、stale 和 unavailable；本轮保存快照成功不代表所有入口都是新读结果。

文件按课程和稳定文件 ID／版本归档，流式下载并计算 SHA256；current 入口与历史原件分开。`audit` 以各课程 `files` 的唯一文件为单位校验，附件元数据重复不重复计数。未解锁、无权限、源删除和提取失败分别报告；旧文件不会因暂时不可访问而删除。必要时对照来源页面核对可读取范围。

需要 Gmail 时，由宿主执行 `gmail-queries` 给出的学校收件人、已知学业发件人、课程号三组互补搜索。原收件人可能保留在转发中，`deliveredto:` 不能单独证明学校范围。首次从学期准备日起，后续在完整搜索水位上回退 72 小时；候选按 message ID 去重，读取相关完整正文，保留分页和筛除依据。部分搜索失败、正文未补齐或解码警告不能推进完整水位。

`gmail-import` 只导入宿主取得的结果，不自行连接邮箱；之后由宿主阅读 `gmail-review-packet` 并维护审阅文件。附件按邮件 ID／附件 ID 保留来源和可访问状态，使用父邮件支持的读取能力；外部共享链接不是正文已读证明，也不是扫描整个云盘的理由。

## 完成判定

核对本次所选来源、文件与保存结果，确认原件可读、来源身份稳定、个人内容保留。使用 Notion 时核对已写入记录、原页面 ID 和关系；明确说明已完成、不可访问与待核实范围。迁移、重启和失败续跑见 [长期维护与恢复](maintenance.md)。
