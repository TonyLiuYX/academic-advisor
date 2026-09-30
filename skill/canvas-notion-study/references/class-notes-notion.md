# Notion 课堂笔记

用户选择将课堂照片或扫描 PDF 的整理结果直接保存到 Notion 时使用。视觉转录由宿主智能体完成，CLI 负责身份、去重、缓存与操作记录。无需独立手写 skill、Obsidian 或额外 OCR 服务。

## 内容约定

- 每课程每学期一篇连续长笔记，复用 Notes 数据库，Type=`Class notes`，保留原 syllabus 笔记。首次有输入才建页；按上课日期和主题追加。
- 沿用已确认课程、学期、语言和 Notion 绑定。多课程分别导入；无法确认课程只暂停相应部分。“今天”使用实例时区；其他无依据日期标记待确认。课节 ID 来自已确认课表，不确定则只关联课程。
- 忠实转录与排版，保留原稿语言、中英混写、推导顺序、批注、例题、实验数据和跨页关系；不主动总结、翻译、补讲、补推导、改错或生成教学图。标题和结构由材料决定。
- 逐页检查公式符号、上下标、条件、界限和单位；代码保持缩进与运算符，不执行源代码；表格区分实测与计算值。双栏、箭头和图形关系不能被错误平铺。图示使用原图或保真裁剪。
- 不清楚的文字、公式、疑似原稿错误使用可见“待确认（来源／页码）”说明并保留原图，不能只写隐藏注释；不臆造缺失部分，其余内容继续。

## 导入

恢复实例，查看 `context`、plan 的 courses Source Key 及 `course-schedule.json`。读取当前连接的 `fetch("self")`、`notion://docs/enhanced-markdown-spec`，确认页读写与 `create_file_upload` 可用。沿用用户已授权的归档目的地，无需每批重复确认。

原稿按阅读顺序输入。PDF 保留原件并用 pages 标注页码；跨课 PDF 先生成保真的分课副本，记录原来源页码。超过连接器／工作区限制时拆分 PDF，明确报告未能保存的原件，不将有损副本称为原件。

在 `state_dir/notes-incoming/` 准备临时 JSON：

```json
{
  "course_key":"从 plan 复制的课程 Source Key",
  "date":"today",
  "topic":"原稿主题",
  "sources":[{"id":"p1","path":"/absolute/photo.png","pages":"1"}],
  "sections":[{"source_ids":["p1"],"markdown":"忠实整理的 Notion Markdown\n\n[[asset:p1]]"}],
  "review_items":[{"source_id":"p1","message":"页 1 的指数不清楚，保留原图待确认。"}]
}
```

可选：`occurrence_id` 来自课表；`date` 为 ISO 日期、today 或 null；`figures` 与 sources 同格式，只放正文确需引用的原图裁剪；`review_items` 无疑点时省略。全新批次可用 `markdown` 代替 sections。每个新来源都需对应一段内容，空白页也需明确说明；部分重传按 source_ids 拆段，避免重复追加。来源图片使用 `[[asset:ID]]`，不要写本地路径、短时 URL 或 Obsidian 链接。

直接编写当前 Notion Markdown：行内数学为 `$` 加反引号包裹公式再加 `$`，块公式为 `$$`；代码块不转义；表格使用支持的 `<table>`。原稿与复核管理信息放子页，不生成 Markdown 包、打印稿或 Obsidian 交付。

```sh
python scripts/study.py --config CONFIG notes-import BATCH.json
python scripts/study.py --config CONFIG notes-next --batch BATCH_ID
```

导入仅准备本地状态。重复文件按课程和 SHA-256 跳过，改顺序不会再次追加；重新拍摄的同一内容仍需人工对照。明确要求修订时传 `revision_of` 指向最新已验证 batch ID，提供修订后的该段内容；未传 sources 时复用该段已保存在 Notion 的原稿。

## 写入与回执

一次处理一个操作；执行前运行 `notes-next --begin --out OP.json`。操作和索引与现有 outbox 同存 `notion-state.json`；一个课程只允许一个待决写入。仅预览而未 begin 的操作不能执行。

| notes_stage | 操作与核验 |
| --- | --- |
| notebook | 使用 payload 在绑定 Notes 中创建主笔记，读回核对课程关系、属性、唯一标记与续写位置。 |
| evidence_page | 使用 payload 创建“原稿与复核记录”，读回确认实际父页为主笔记。 |
| asset | 按输出缓存路径上传原稿并实际附加到原稿子页；操作方法见下文。 |
| evidence_entry | 使用 payload 追加本批日期、来源顺序和复核问题，读回。 |
| entry / revision | 使用 payload 定点追加／修订主笔记，读回逐项对照正文、公式、图示和来源。 |

`asset`：核对缓存 SHA-256，调用 `create_file_upload`，按其文档向短时 URL 发送一次 multipart POST。立即用 `notes-receipts` 保存中间回执（source_key、operation_id、status=`uploaded`、upload_id）；它仅保存文件身份，操作仍为待决，不能当成成功。将响应 `suggested_markdown` 放入子页：使用 `update_content` 将唯一 `old_str` 替换为 `start_marker`、来源 ID／页码／SHA-256、附件 Markdown、`end_marker`、`new_anchor`，各项间空行。实际 fetch 并打开／检查附件可访问后才能交成功回执。上传 URL 和 headers 不写入日志、报告或发布包。上传字节成功不等于原稿已保存到页面。

遇到 `fetch_required`，读取指定页，保存为以下处理缓存，然后加 `--readbacks READBACKS.json`；只需包含所需角色：

```json
{"main":{"page_id":"实际 ID","content":"实际读回的页面正文 Markdown","truncated":false,"unknown_block_count":0},
 "evidence":{"page_id":"实际子页 ID","content":"实际正文","truncated":false,"unknown_block_count":0}}
```

content 是 fetch 返回 `<content>` 内正文，不能是 JSON wrapper。完整性字段必须反映实际结果；长页截断或未知区块时保留待核验状态，不能填成完整或宣称归档成功。原稿的 Notion 文件身份用于正文图片；已经附加到 Notion 的身份可复用。若无法复用，保留待决状态并说明附件需要修复，不能盲目重传或手改已核验索引。页面中的短时签名图片 URL 不存为长期地址；修订哈希忽略这些签名的变化，但仍核对实际对象和正文。

实际对照完成后提交：

```json
{"receipts":[{
  "source_key":"操作原值","operation_id":"操作原值","status":"succeeded","content_verified":true,
  "readback":{"page_id":"实际 ID","content":"实际读回正文","truncated":false,"unknown_block_count":0}
}]}
```

`evidence_page` 另传实际 `parent_page_id`；`asset` 另传 `upload_id` 和 `attachment_verified:true`，仅在实际核对附件后填写（已保存 uploaded 回执可省略 upload_id）。PDF 读回可能是 `file://%7B…` 的 Notion 原生附件引用，而非本机路径；若上传身份丢失，可把实际完整读回中的原生引用作为 `remote_source`，仍须核对附件可访问。`content_verified` 是宿主对照后的声明，不是 CLI 自动保证了学术正确性。Notion 会规范化数学、链接和空行，修订基线使用实际读回段落哈希。

```sh
python scripts/study.py --config CONFIG notes-receipts RECEIPTS.json
```

## 中断、修订与完成

`inflight_requires_reconciliation` 给出待决操作供核对，不得直接重放。异步工具先等待完成；新建结果不明时按 Source Key／唯一标记查找并读取。已写入则比较段落、附件与进度后补成功回执。确认写入未发生后，才能交 `status:definitely_not_applied`、`confirmed_absent:true` 和具体 reason，再重试。重复页面、未知附件或多义定位均暂停该操作。

修订只替换指定段落；远端段落被用户修改时哈希检查报冲突，保留用户内容并请用户明确如何合并。普通 Canvas 同步不重投影长笔记。未决操作和失败批次保留恢复缓存。

成功回执落盘后，程序清理自己产生的该批原稿副本和草稿；用户原文件保留。宿主随后清理本次自己产生且不再需要的 notes-incoming、readback、payload 临时文件，不删除用户文件或未完成批次。本地长期仅保留索引、哈希、映射、复核问题及必要回执。

`context.recent_notes` 提供近期笔记和链接。已确定课节的笔记注明课节及已有安排链接，并接入同系列下一次课已有回顾的材料入口；后续普通同步会把这些笔记链接更新到已有准备任务正文，仍需使用原有内容冲突检查。未确定课节则只进入课程索引。不因上传笔记改变 Done、Planned、学习会话、掌握度或官方要求，不生成待办。

运行 `notes-next --batch BATCH_ID` 确认无待执行项，逐份核对原稿与正文。最后返回 Notion 链接，说明追加／修订／跳过重复及尚待确认的文字。文字存有疑点与远端保存失败是不同状态，分别如实报告。
