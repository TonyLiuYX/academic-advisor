"""Export browsable local files, announcements, replies, and course pages."""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

from .archive import safe_component
from .projection import tasks_to_ics, render_generated_content
from .state import read_json, write_json


def clean_url(value):
    parts = urlsplit(str(value or ""))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def label(value):
    return str(value or "").replace("|", "\\|").replace("\n", " ").replace("[", "\\[").replace("]", "\\]")


def _escape(value):
    return html.escape(str(value or ""), quote=True)


def _relative(path, parent):
    return quote(os.path.relpath(path, parent), safe="/")


def _document(title, body):
    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_escape(title)}</title><style>
:root{{color-scheme:light dark;--bg:#f6f5ef;--ink:#24342e;--muted:#64736c;--line:#d8ded5;--card:#fffef9;--accent:#27664c}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:16px/1.75 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
main{{max-width:1060px;margin:auto;padding:44px 24px 80px}}h1{{font-size:clamp(28px,5vw,42px);line-height:1.3}}h2{{margin-top:32px;font-size:23px}}
a{{color:var(--accent);text-underline-offset:3px;overflow-wrap:anywhere}}nav,.meta{{font-size:14px;color:var(--muted)}}
.card,article{{padding:24px;background:var(--card);border:1px solid var(--line);border-radius:12px;margin:16px 0}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px}}.grid .card{{margin:0}}.card h2{{margin:0 0 12px}}
.table-wrap{{overflow-x:auto}}table{{border-collapse:collapse;width:100%}}th,td{{padding:12px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}}
.body{{overflow-wrap:anywhere}}img{{max-width:100%;height:auto}}pre{{white-space:pre-wrap;overflow-wrap:anywhere}}
.reply,blockquote{{border-left:3px solid var(--line);padding-left:20px;margin:20px 0}}
@media(prefers-color-scheme:dark){{:root{{--bg:#17211d;--ink:#e5ece7;--muted:#acbdb2;--line:#3c4e43;--card:#202d26;--accent:#9ed7b7}}}}
</style></head><body><main>{body}</main></body></html>
'''


class _BodyRenderer(HTMLParser):
    """Keep ordinary document markup and rewrite archived file references."""
    tags = set("p div span br hr h1 h2 h3 h4 h5 h6 strong b em i u s ul ol li blockquote pre code table thead tbody tfoot tr td th sup sub a img".split())
    void = {"br", "hr", "img"}
    skip = {"script", "style", "template", "noscript"}

    def __init__(self, source, parent, file_links):
        super().__init__(convert_charrefs=True)
        self.source, self.parent, self.file_links = source, parent, file_links
        self.parts, self.skipping = [], 0

    def link(self, value):
        if not value:
            return ""
        absolute = urljoin(self.source, value)
        parsed, origin = urlsplit(absolute), urlsplit(self.source)
        match = re.search(r"(?:^|/)files/(\d+)(?:/|$)", parsed.path)
        if match and (parsed.scheme, parsed.netloc) == (origin.scheme, origin.netloc):
            target = self.file_links.get(match.group(1))
            if target:
                return _relative(target, self.parent)
        return absolute if parsed.scheme in {"http", "https", "mailto"} else ""

    def handle_starttag(self, tag, attrs):
        if tag in self.skip:
            self.skipping += 1
        if self.skipping or tag not in self.tags:
            return
        attributes, output = dict(attrs), []
        if tag in {"a", "img"}:
            key = "href" if tag == "a" else "src"
            value = self.link(attributes.get(key) or "")
            if value:
                output.append(f'{key}="{_escape(value)}"')
            if tag == "img":
                output.append(f'alt="{_escape(attributes.get("alt") or "课程图片")}"')
        for key in ("colspan", "rowspan") if tag in {"td", "th"} else ():
            if str(attributes.get(key, "")).isdigit():
                output.append(f'{key}="{attributes[key]}"')
        self.parts.append("<" + tag + (" " + " ".join(output) if output else "") + ">")

    def handle_endtag(self, tag):
        if tag in self.skip:
            self.skipping = max(0, self.skipping - 1)
        elif not self.skipping and tag in self.tags and tag not in self.void:
            self.parts.append(f"</{tag}>")

    def handle_data(self, data):
        if not self.skipping:
            self.parts.append(_escape(data))


def _body(record, source, parent, file_links):
    raw = next((record.get(k) for k in ("message", "body", "description") if record.get(k)), None)
    if raw is None:
        return '<div class="body">' + _escape(record.get("text") or "正文尚未获取。").replace("\n", "<br>") + "</div>"
    renderer = _BodyRenderer(source, parent, file_links)
    renderer.feed(str(raw))
    renderer.close()
    return '<div class="body">' + "".join(renderer.parts) + "</div>"


def _attachments(record, source, parent, file_links):
    values = record.get("attachments") or []
    values = [values] if isinstance(values, dict) else list(values)
    if isinstance(record.get("attachment"), dict):
        values.append(record["attachment"])
    links, seen = [], set()
    resolver = _BodyRenderer(source, parent, file_links)
    for item in values:
        if not isinstance(item, dict):
            continue
        key = str(item.get("id") or item.get("source_id") or item.get("url") or "")
        if key in seen:
            continue
        seen.add(key)
        target = file_links.get(str(item.get("id")))
        url = _relative(target, parent) if target else resolver.link(str(item.get("url") or item.get("source_url") or ""))
        name = item.get("display_name") or item.get("filename") or item.get("name") or "附件"
        links.append(f'<li><a href="{_escape(url)}">{_escape(name)}</a></li>' if url else f"<li>{_escape(name)}（尚未保存）</li>")
    return "<h3>附件</h3><ul>" + "".join(links) + "</ul>" if links else ""


def _replies(replies, source, parent, file_links, seen=None):
    seen, parts = set() if seen is None else seen, []
    for reply in replies or []:
        if not isinstance(reply, dict):
            continue
        key = str(reply["id"]) if reply.get("id") is not None else None
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        user = reply.get("user") or {}
        author = reply.get("user_name") or reply.get("author_name") or user.get("display_name") or user.get("name") or "回复"
        stamp = reply.get("created_at") or reply.get("updated_at") or ""
        parts.append(f'<section class="reply"><p class="meta">{_escape(author)} · {_escape(stamp)}</p>' + _body(reply, source, parent, file_links) + _attachments(reply, source, parent, file_links) + _replies(reply.get("replies"), source, parent, file_links, seen) + "</section>")
    return "".join(parts)


def _current_link(current, filename, target, previous, generated):
    """Only replace our recorded links; retain user files and user links."""
    name, suffix = filename, 1
    while True:
        path, relative = current / name, os.path.relpath(target, current)
        if not path.exists() and not path.is_symlink():
            path.symlink_to(relative)
            break
        if path.is_symlink() and os.readlink(path) in {previous.get(name), generated.get(name)}:
            if os.readlink(path) != relative:
                path.unlink()
                path.symlink_to(relative)
            break
        suffix += 1
        source = Path(filename)
        name = f"{source.stem}-{suffix}{source.suffix}"
    generated[name] = relative
    return path


def export_workspace(snapshot: dict, plan: dict, archive_dir: str | Path) -> dict:
    root = Path(archive_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    term = snapshot["term"]
    term_label = term.get("label") or term.get("key") or "课程资料"
    course_records = {str(r["properties"].get("Canvas ID")): r for r in plan.get("records", []) if r["kind"] == "courses"}
    manifest = {"schema_version": 2, "term": term, "generated_at": snapshot.get("generated_at"), "files": [], "warnings": [], "courses": []}
    lines = [f"# {term_label} 课程资料", "", "[在浏览器中打开资料入口](index.html)", "", "原件按来源及版本保存；每门课程的 current 文件夹提供本次可用文件。", "", "| 课程／空间 | 文件索引 |", "| --- | --- |"]
    cards = []
    for course in snapshot.get("courses", []):
        if course.get("mode") not in {"course", "hub"}:
            continue
        cid, record = str(course["id"]), course_records.get(str(course["id"]))
        name = record["properties"]["Name"] if record else course["name"]
        code = record["properties"].get("Course Code") if record else course.get("course_code") or (f"hub-{cid}" if course.get("mode") == "hub" else cid)
        slug = safe_component(code or cid) + "-" + safe_component(cid)
        folder = root / "courses" / slug
        current = folder / "current"
        current.mkdir(parents=True, exist_ok=True)
        previous = read_json(folder / ".generated-links.json", {})
        generated, file_links, file_rows = {}, {}, []
        origin = snapshot.get("canvas_origin") or snapshot.get("base_url") or ""
        course_url = clean_url(course.get("source_url") or course.get("html_url") or str(origin).rstrip("/") + "/courses/" + cid)
        local = [f"# {code} · {name}", "", "[浏览器目录](index.html)", "", f"来源：[Canvas 课程空间]({course_url})", "", "本页由同步生成。个人笔记请另建文件。", ""]
        if record:
            local.extend([render_generated_content(record), ""])
        local.extend(["## 原始文件", "", "| 文件 | 本地原件 | 状态 |", "| --- | --- | --- |"])
        available = 0
        for item in course.get("files", []):
            path = Path(item["local_path"]).expanduser() if item.get("local_path") else None
            valid = bool(path and path.is_file())
            if valid and item.get("sha256"):
                valid = hashlib.sha256(path.read_bytes()).hexdigest() == item["sha256"]
                if not valid:
                    manifest["warnings"].append({"course_id": cid, "file_id": item.get("id"), "reason": "local_checksum_mismatch"})
            title = item.get("display_name") or item.get("filename") or str(item.get("id"))
            status, shortcut = item.get("download_status", "unknown"), None
            if valid:
                available += 1
                file_id = str(item.get("id") or item.get("source_id") or hashlib.sha256(str(path).encode()).hexdigest()[:12])
                shortcut_name = safe_component(file_id + "-" + str(title), max_length=165)
                if not Path(shortcut_name).suffix and path.suffix:
                    shortcut_name += path.suffix
                shortcut = _current_link(current, shortcut_name, path, previous, generated)
                file_links[file_id] = shortcut
                local_link = f"[打开]({_relative(shortcut, folder)})"
                html_link = f'<a href="{_relative(shortcut, folder)}">打开文件</a>'
            else:
                local_link = html_link = "未保存成功"
            source = clean_url(item.get("source_url"))
            title_link = f"[{label(title)}]({source})" if source else label(title)
            local.append(f"| {title_link} | {local_link} | {label(status)} |")
            file_rows.append(f"<tr><td>{_escape(title)}</td><td>{html_link}</td><td>{_escape(status)}</td></tr>")
            manifest["files"].append({"course_id": cid, "id": item.get("id"), "name": title, "source_url": source, "local_path": str(path) if path else None, "current_path": str(shortcut) if shortcut else None, "sha256": item.get("sha256"), "download_status": status, "extraction_status": item.get("extraction_status"), "local_hash_verified": valid, "discovered_from": item.get("discovered_from") or item.get("discovered_on") or []})
        for old_name, old_target in previous.items():
            old_path = current / old_name
            if old_name not in generated and old_path.is_symlink() and os.readlink(old_path) == old_target:
                old_path.unlink()
        write_json(folder / ".generated-links.json", generated)

        sections, documents = [], {"announcements": [], "pages": []}
        for kind, heading in (("announcements", "课程通知"), ("pages", "网页资料")):
            local.extend(["", "## " + heading, ""])
            items = []
            for item in course.get(kind, []):
                item_id = str(item.get("id") or item.get("page_id") or item.get("url") or hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()[:12])
                title = item.get("title") or item.get("name") or item_id
                page_path = folder / kind / (safe_component(item_id, max_length=100) + "-" + hashlib.sha256(item_id.encode()).hexdigest()[:8] + ".html")
                page_path.parent.mkdir(parents=True, exist_ok=True)
                source = item.get("source_url") or item.get("html_url") or course_url
                stamp = item.get("posted_at") or item.get("created_at") or item.get("updated_at") or ""
                body = f'<nav><a href="../index.html">← {_escape(code)} 课程目录</a></nav><h1>{_escape(title)}</h1><p class="meta">{_escape(stamp)} · <a href="{_escape(clean_url(source))}">Canvas 原文</a></p><article>'
                body += _body(item, source, page_path.parent, file_links) + _attachments(item, source, page_path.parent, file_links) + "</article>"
                if kind == "announcements":
                    replies = _replies(item.get("replies"), source, page_path.parent, file_links)
                    if replies:
                        body += "<h2>已读取回复</h2>" + replies
                    elif item.get("discussion_subentry_count", 0):
                        body += '<p class="meta">Canvas 显示此通知有回复，本地快照中尚无可读回复内容。</p>'
                page_path.write_text(_document(title, body), encoding="utf-8")
                target = _relative(page_path, folder)
                items.append(f'<li><a href="{target}">{_escape(title)}</a> <span class="meta">{_escape(stamp)}</span></li>')
                local.append(f"- [{label(title)}]({target})")
                documents[kind].append({"id": item_id, "title": title, "local_path": str(page_path), "source_url": clean_url(source)})
            sections.append(f"<h2>{heading}</h2><ul>" + "".join(items) + "</ul>" if items else f'<h2>{heading}</h2><p class="meta">本次未读取到记录，请结合覆盖情况查看。</p>')
        if course.get("resources"):
            sections.append("<h2>外部资料入口</h2><ul>" + "".join(f'<li><a href="{_escape(item.get("source_url"))}">{_escape(item.get("title") or item.get("source_url"))}</a>（链接入口）</li>' for item in course["resources"]) + "</ul>")
        if course.get("warnings"):
            local.extend(["", "## 本次未覆盖与提示", ""])
            warnings = []
            for warning in course["warnings"]:
                message = f"{warning.get('code')}: {warning.get('message')}（对象 {warning.get('object_id', '')}）"
                local.append("- " + message)
                warnings.append("<li>" + _escape(message) + "</li>")
            sections.append("<h2>本次未覆盖与提示</h2><ul>" + "".join(warnings) + "</ul>")
        coverage = course.get("coverage") or {}
        if coverage:
            sections.append('<h2>采集覆盖情况</h2><details><summary>查看各入口的读取状态</summary><pre>' + _escape(json.dumps(coverage, ensure_ascii=False, indent=2)) + "</pre></details>")
        course_body = f'<nav><a href="../../index.html">← 全部课程</a></nav><h1>{_escape(code)} · {_escape(name)}</h1><p class="meta"><a href="{_escape(course_url)}">Canvas 课程空间</a> · {available} / {len(course.get("files", []))} 份文件可在本地打开</p><h2>本次文件</h2><p class="meta">current 文件夹链接到本次快照中的可用原件；preserved 表示上次保存的版本。</p><div class="table-wrap"><table><thead><tr><th>资料</th><th>本地文件</th><th>状态</th></tr></thead><tbody>' + "".join(file_rows) + "</tbody></table></div>" + "".join(sections)
        (folder / "index.html").write_text(_document(name, course_body), encoding="utf-8")
        (folder / "README.md").write_text("\n".join(local) + "\n", encoding="utf-8")
        manifest["courses"].append({"id": cid, "name": name, "discovered_files": len(course.get("files", [])), "verified_local_files": available, "coverage": coverage, "html_index": str(folder / "index.html"), **documents})
        lines.append(f"| {label(code)} · {label(name)} | [打开索引](courses/{quote(slug)}/README.md) |")
        cards.append(f'<section class="card"><h2><a href="{_relative(folder / "index.html", root)}">{_escape(code)}</a></h2><p>{_escape(name)}</p><p class="meta">{available} 份本地文件 · {len(documents["announcements"])} 条通知 · {len(documents["pages"])} 页资料</p></section>')
    lines.extend(["", "[任务截止日期日历](tasks.ics)仅包含已有明确日期、且没有日期冲突的任务。", "", "manifest.json 记录文件来源、发现位置、覆盖状态和校验结果。"])
    (root / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (root / "index.html").write_text(_document(term_label + " · 课程资料", f'<p class="meta">本地课程资料 · {_escape(snapshot.get("generated_at") or "")}</p><h1>{_escape(term_label)}</h1><p>按课程打开文件、通知和网页资料。</p><div class="grid">' + "".join(cards) + '</div><p><a href="tasks.ics">任务截止日期日历</a> · <a href="manifest.json">资料清单与覆盖记录</a></p>'), encoding="utf-8")
    (root / "tasks.ics").write_text(tasks_to_ics(plan, calendar_name=term_label + " · Canvas 任务"), encoding="utf-8", newline="")
    write_json(root / "manifest.json", manifest)
    return {"index": str(root / "README.md"), "html_index": str(root / "index.html"), "manifest": str(root / "manifest.json"), "calendar": str(root / "tasks.ics"), "files": len(manifest["files"]), "verified": sum(bool(f["local_hash_verified"]) for f in manifest["files"]), "warnings": manifest["warnings"]}
