# 理工科课程笔记：Obsidian 正文与 Notion 索引

用户选择 Obsidian 时使用独立 `course-notes` 核心，按需绑定 vault、课程、学期和语言；导师实例使用 `notes.backend: obsidian`。Notion 索引可选。用户选择直接保存到 Notion 时，按 [Notion 课堂笔记](class-notes-notion.md) 执行。

## 整理与追加

1. 有导师实例时恢复配置与课程 Source Key，用 `notes-find` 读取正文和相关单元；独立整理笔记时直接使用 course-notes 的 notebook locate。尚未绑定时通过 notebook bind 建立一课程一学期一篇主笔记。
2. 按 course-notes 的 STEM 规范逐页检查原稿。默认讲义式增强、visual-rich，区分原始记录、AI explanation、补充例题和原稿疑点。结构与材料课程强调对象、模型、假设、受力、单位和工程解释；微积分强调条件和函数图；线性代数同时呈现运算与几何。语言使用实例 notes.language，不写死为中文或英文。
3. 主动规划丰富配图。原生 Mermaid/MathJax/表格/Callout 表达适合它们的信息，精确 SVG 承载数量与受力关系，积极用多幅 AI 图解释工程场景、复杂空间结构和概念。默认简洁教材风格：白底、清晰线条、少量配色，避免写实与装饰性背景；尊重实例 visual_style。一个主题可以同时具备 SVG 分析图与 AI 直观图。没有生图能力时记录提示词及未完成状态。
4. 主笔记在 `Courses/TERM/COURSE/COURSE.md`，原稿与生成图按批次永久保留。全部链接使用相对路径。稳定 note_id/unit ID 与可见编号分开；来源只确认 Week 1–2 时不捏造具体日期或 Lecture 编号。
5. 使用 `math2obsidian notebook append` 预览并核对正文；携带预览 expected_sha 应用。修订单元先合并个人文字，再提供当前 base_sha256。重复上传和远端失败均不重复追加本地正文。
6. 在 Obsidian 实际检查阅读模式、Live Preview、颜色、编号、长公式、图示和锚点。源稿缺失部分保持可见待完成状态。

## 自动定位与读取

```sh
python scripts/study.py --config CONFIG notes-find --course COURSE --topic TOPIC --read
python scripts/study.py --config CONFIG notes-find --note-id NOTE-ID --read
```

先用 Notes 中 Note ID / Vault Key / Note Path 或本地 state 的 obsidian 记录定位。核心验证正文 note_id，路径变化时在已登记 vault 搜索。missing/ambiguous 必须显式报告；不能把同名文件当成已找到。URI 用于打开应用，agent 从解析后的本地路径读取正文。只用课程与主题也能查询；同名课程跨学期需指定 term。context.recent_notes 与课前准备材料提供 note_id、单元锚点及读取命令；给建议之前读取实际相关单元，缓存目录不是正文依据。

## Notion 索引同步

仅在用户选用 Notion 索引时执行本节；Obsidian 正文与原稿可以独立保存。

```sh
python scripts/study.py --config CONFIG notes-index --course-key COURSE-SOURCE-KEY --note-id NOTE-ID
python scripts/study.py --config CONFIG notes-next
```

notes-index 核验本地笔记并排队远端索引。宿主使用连接器更新已有 Notes 页的 Storage、Note ID、Vault Key、Note Path、Obsidian URI、Last Indexed、Link Status；沿用旧 page_id 和 Course 关系，不覆盖个人字段或来源字段。新用户尚无页面时，在绑定的 Notes 数据库创建一条 Class notes 索引，填入课程关联并保存实际 page_id 到相应 notebook 记录。

将已有 Notion 笔记转到 Obsidian 时，重新 fetch 当前主页面及原稿子页，先保存快照。取得原稿可读副本、检查内容后，再在原页顶部添加 Obsidian 入口和单元目录，将旧正文标为历史副本。保留原稿子页，不做云端全文镜像。首页和课程 Notes 视图显示正文位置、最近索引时间及定位状态。

对支持 Markdown 富文本的连接器，Note Path 和 Obsidian URI 可用行内代码格式防止 `.md` 被自动识别为网站。部分连接器会移除正文的 `obsidian://` 超链接；必须在入口保留完整 URI 代码文本，未实际验证不能声称一键可点击。目录保留可读标题与稳定单元 ID；列表项使用 `Unit 3 — Title`，避免以 `3. Title` 开头而被解析成嵌套有序列表。追加后更新已有目录，不重复插入入口。

写入后实际 fetch，再将 `{page_id, properties}` 保存为读回 JSON，运行下面的命令。适配器接受实际 Notion 富文本属性及其 Markdown 自动链接序列化，比较显示文字并保留 URI 的百分号编码，不能把超链接地址当成本机路径。

```sh
python scripts/study.py --config CONFIG notes-index --course-key COURSE-SOURCE-KEY --note-id NOTE-ID --readback READBACK.json
math2obsidian notebook sync-receipt --note-id NOTE-ID --batch-id BATCH-ID --readback PROPERTIES.json
```

Notion 失败时保留本地正文、永久附件和 pending 索引；恢复时只重试元数据并读回，不重放正文追加。成功之后再记录 verified。Obsidian 的 assets 永久保留。

## 实例配置

配置可包含 `notes: {backend: "obsidian", language: "en", registry: "本机注册表路径", user: "default", command: ["math2obsidian"]}`。command 是 argv 数组；可指定本机已安装的可执行文件路径。机器路径只留在私有本地配置中，不进入 Notion、共享 skill 或发行包。

不因归档笔记推断 Done、掌握度、官方要求或新待办；这一点适用于全部后端。
