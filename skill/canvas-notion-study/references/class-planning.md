# 按课节安排学习

本功能规划“要做什么、何时做”，不承担讲题、出题、答题诊断或掌握度评分。粒度为一次课前准备、一组课程推荐练习、一次备考。

## 来源与持久输入

实例 `state/course-schedule.json` 是宿主阅读课程材料与个人课表后保存的输入，`schedule-state.json` 是程序维护的规则任务身份账本。不要把 Timetable 中所有活动直接转换为上课记录。用户课表确认个人班别；课程通知提供取消、调课、材料及独立交付要求。个人截图归档到本实例 `materials/personal/`，来源路径与课程链接一并留痕。

`series` 每项包含稳定 `id`、`course_key`、`kind`（lecture/lab/pra/tutorial）、`confirmed`、`source_refs`、`source_url`。循环课节填写 `start_date`、`end_date`、`weekdays`（周一0到周日6）；每天不同钟点用 `times_by_weekday`（键为星期数字字符串，值含 start_time/end_time）。可统一提供 start_time/end_time。时区沿用实例。起止日期应来自学期和课程材料。

支持 `interval_weeks`、`exclude_dates`；`exceptions` 按原始课节日期或稳定 occurrence id 覆盖实际 date/time/kind、取消状态和本次材料。调课保留原始身份。额外补课用 `additional_occurrences`，含稳定 id/date；不在未证实使用补课日时自动补课。也可用 `occurrences` 指定非循环的完整课节列表，每项含稳定 id/date。

日期已知但钟点未知时，可留空时间；未确认个人适用日期时 confirmed=false，已知来源任务继续保留。不可根据相邻班级猜测本人时间。

系列或具体课节可附 `resource_keys`、`study_steps`、`linked_task_keys` 和 `available_from`。步骤使用稳定 id；内容来自已读材料。没有明确准备要求时，使用程序的轻量默认提示；材料尚未发布时只浏览已发布内容，不假装读过。不要要求为课前准备完成老师只要求在lab现场做的题目。

## 复用与任务类别

`bindings` 使用 occurrence_id（系列id:原始日期或课节id）关联已有 `study-preparations.json` 中的 preparation_id，或者直接 canonical_task_key。例如 `{"occurrence_id":"COURSE:LAB01:2026-09-16","preparation_id":"lab-preparation"}`。先检查现有任务，避免把同一准备再生成一遍。独立的 pre-lab／报告保持原任务与Due，用 linked_task_keys关联。

准备 origin 可为 Teacher requirement、Teacher recommendation、Assistant suggestion。旧准备默认沿用教师要求分类；没有依据不能把助手模板升级为教师要求。助手建议不进入官方必办要求账本；教师推荐默认参与学习安排，但可选行政活动仍不自动进入。

`recommendations` 每项含稳定 id、course_key、title、study_date、source_refs，可附资源和步骤；同一课程同周同一材料只生成一次。可用 preparation_id或canonical_task_key复用原项。列教师推荐练习集和已公布章节即可，不必分析或辅导每道题。

`assessments` 每项含稳定 id、course_key、title、kind（quiz/midterm/final）、date、source_refs；可关联 canonical_task_key 获取最新官方日期，并通过 preparation_id复用已存在的备考项。必须阅读来源来分类，不能只看任务标题猜测是否考试。日期冲突或未公布时记入缺口，不猜日期。Quiz默认提前2天；midterm/final提前7天和2天。可在输入中设置 quiz_lead_days/exam_lead_days/exam_final_lead_days。最后复习可在临时公布时安排到今天，但不创建已过去的首次备考阶段。

## 运行与更新

沿用原来源更新和个人字段读回流程。审阅新材料后同步修订课表输入、本次材料或复用关系。`plan --week YYYY-MM-DD` 生成目标周及后两周的规则任务，默认当前周；`week-plan --week ...` 会以同一基准重建，`study-start` 会更新当前周规则。日常使用当前时间。

Due始终为官方截止，Study Date为建议日，Planned为个人安排。Prepare Before保存对应课节／考核边界。未知空闲时间只输出日期草案；明确时段先扣除本人上课时间，再安排80%容量；不会在相关课节结束后安排课前准备，也不提前安排对尚未进行的上次课的回顾。个人安排与课节冲突时保留原值并提示。

规则任务以课程＋原始课节／考核身份生成稳定key，调课与改名更新原页面。未完成的常规回顾可合并到同系列的下一次准备，旧项保留Merged及去向，不勾选Done。取消、过去或后续周的建议退出当前候选；来源要求不因规则退出而消失。不要删除 schedule-state.json，否则会丢失合并和取消历史。

使用 Notion 时复用 Tasks 与 Weeks，通过 Planning Origin、Learning Activity、Preparation Status、Class / Assessment 和 Prepare Before 保存准备信息；按Study Date展示学习安排，原Due日历继续表示提交截止。使用同一outbox、回执和个人内容保护流程。使用 Notion 时读取实际个人进度，否则沿用本地保存的进度；新对话通过context和完整课表／规则账本恢复。

## 结果核对

周页还需核对七天是否齐全、课节时间顺序、周日边界，以及下述版式；内容重排不改变任务身份、日期或个人状态。

对照个人课表核对所选课程的课节、准备事项、调课与取消状态；保留教师推荐、独立提交要求、未发布材料和日期冲突的说明。更新时复用已有准备任务并保留个人字段。

## 周计划版式

- 一周固定为周一至周日。主周展开；下一周、再下一周各自用带日期范围的折叠区，不能合成一条“后两周”长清单。每周显示完整七天，周日后用分隔线结束。
- 每天以“周二 · 9月29日”等标题分组。课节按实际开始时间排序，以三列表格展示“当天课节／课前准备／准备日期”。课节写课程、lecture/lab/PRA/tutorial、已确认钟点和教室；未确认的字段保留待确认。已取消的课节标注停课，不列为需要准备。
- 准备内容从现有任务步骤与已审阅课表读取；教师要求、教师推荐、助手建议清楚标注。无专属材料时只给轻量且明确标作助手建议的准备提示。已过去的课节可展示当时要求，不推断任务完成，也不把已合并的建议重新激活。
- 课节按上课日归组；准备日期保留原建议日或个人计划日，可能位于前一周日。其他学习和提交事项按个人计划日、建议日、已知截止日依次归组。官方截止与准备日分别标示，日期冲突保持待核实；不用排序改变任何原日期。
- 时间使用实例时区，正文显示“9月30日（周三）09:00”，不用原始 ISO 时间串。未提供可用时段时只安排到日期，不虚构学习钟点；日期型截止不补写午夜。
- 原生任务引用会自动显示完整标题，引用前不再抄一次长标题。具体材料和完成标准收在当天折叠区；完成状态继续在原任务记录里维护，周页不创建另一套独立勾选框。
- 往周未完成、无日期、远期及历史事项分别折叠。主周不夹带往周准备；所有原任务入口仍可找到。用户正文、子页、关联视图原位保留。

仅重排时运行 `study.py --config 配置路径 week-render`，或用 `--week YYYY-MM-DD` 选择已存在的一周。命令仅读取本地课表、计划、学习状态，增加展示所需信息并重建周页内容；不采集来源、不重算建议日、不修改个人字段或课程任务。然后只应用 `notion-next --kind weeks --begin` 的差异。普通 `week-plan` 生成的新周也采用同一版式。
