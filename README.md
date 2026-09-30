# 学业规划助手 V1

把课程资料、学习安排、课堂笔记和进度接起来。Skill 负责阅读材料并与你交流，本地脚本保存来源和学习状态，日常工作按你选用的工具展开。

## 工具按需配置

| 工具 | 用途 |
| --- | --- |
| Canvas | 整理课程资料、通知、作业和日程 |
| Gmail | 补充指定范围内的学业邮件 |
| Notion | 查看任务、周计划、学习记录和笔记索引，也可直接保存笔记 |
| Obsidian | 按课程积累正文、原稿和配图，配合独立 `course-notes` 核心使用 |

按需要连接工具，无需一次配齐。直接处理你选择的课程；只整理课堂笔记时，使用选定的笔记路径即可。

## 日常怎么用

- “更新我的学期资料，整理本周目标。”
- “我今晚有 45 分钟，第 2 题还没做完，建议先做什么？”
- “这是今天的课堂笔记，追加到这门课。”
- “这次做了 30 分钟，第 2 题还卡着，记录进度。”
- “接着我上次的进度继续。”

周计划结合实际课表、课程要求和明确的可用时间，安排课前准备、推荐练习与备考。时间未知时先给完整草案；已确认时段扣除上课后默认安排 80% 容量。官方截止、建议学习日期、个人计划、提交状态和完成反馈分别保存。

Obsidian 笔记按课程与学期持续追加，保留原稿、公式、图示与来源，并明确标注补充讲解。Notion 可以作为课程索引；直接保存到 Notion 的笔记使用对应的归档流程。笔记归档和学习时长不会自动改变任务完成状态。

## 安装与保存位置

可安装内容位于 [skill/canvas-notion-study](skill/canvas-notion-study/SKILL.md)。将整个目录放入支持 Skills 的宿主的 skills 目录。例如在 Codex 中：

```sh
git clone https://github.com/TonyLiuYX/academic-advisor.git
cd academic-advisor
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
cp -R skill/canvas-notion-study "${CODEX_HOME:-$HOME/.codex}/skills/"
```

安装后可以这样开始：

> 使用 $canvas-notion-study，帮我搭建本学期的学习安排。我想使用……，资料保存在……。

本地脚本使用 Python 3.10+。PDF、Word、PowerPoint 的文本提取分别按需使用 `pypdf`、`python-docx`、`python-pptx`，优先使用宿主已有组件。选择 Obsidian 课程长笔记时，按独立 [course-notes](https://github.com/TonyLiuYX/handwritten-math-to-obsidian) 的说明安装笔记核心和 Skill；其他流程按所选功能配置。

首次使用确认对应流程需要的账户、学期、时区和保存位置。Canvas 使用个人账户凭证，保存到 macOS 钥匙串或进程环境；实例配置保存非敏感设置。

| 位置 | 内容 |
| --- | --- |
| `skill/canvas-notion-study/` | Skill 入口、必要参考和执行脚本 |
| `development/` | 开发用检查脚本和合成测试 |

实例配置、课程原件、学习状态与同步记录保存在用户选定的本地目录，例如 `learning-data/`。

## 持续使用

每次对话从实例登记找到同一学期，再读取个人资料、本周任务、最近反馈与来源状态。保存的新进度可在下一次对话继续使用。来源更新保留个人笔记、勾选和计划。

操作方法见[长期维护](skill/canvas-notion-study/references/maintenance.md)，数据流程见[导师工作流](skill/canvas-notion-study/references/workflow.md)。定时运行按需要单独配置。

## 开发检查

在仓库根目录运行：

```sh
python3 development/run_tests.py
```

检查使用隔离的合成数据，在临时目录中运行。

## 许可证

本项目采用 [MIT License](https://github.com/TonyLiuYX/academic-advisor/blob/main/LICENSE)。
