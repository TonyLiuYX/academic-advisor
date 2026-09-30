"""Declarative linked views for connected Notion tools; no remote writes."""
from __future__ import annotations
import json


def view_definitions(plan: dict, state: dict, config: dict) -> list[dict]:
    notion=config.get('notion',{});dbs=notion.get('databases',{});existing=notion.get('views',{})
    term=json.dumps(plan['term']['key']);views=[]
    def add(key,parent,kind,name,type_,configure):
        if not parent or kind not in dbs:
            return
        views.append({'key':key,'existing_view_id':existing.get(key), 'kind':kind,'heading':name,'arguments':{'parent_page_id':parent,'data_source_id':dbs[kind]['data_source_id'],'name':name,'type':type_,'configure':configure}})
    root=notion.get('root_page_id');archive=notion.get('archive_page_id') or notion.get('admin_page_id') or root;tf=f'FILTER "Term" = {term}; '
    scoped='Scope' in plan.get('databases',{}).get('tasks',{}).get('properties',{})
    active=('FILTER "Scope" != "Historical" AND "Scope" != "Optional" AND "Scope" != "Reference"; '
            'FILTER ("Canvas Status" != "graded" AND "Canvas Status" != "submitted" AND "Canvas Status" != "pending_review") OR "Evidence Status" = "partial_completed"; '
            'FILTER "Evidence Status" != "confirmed_completed"; ' if scoped else '')
    learning = 'Planning Origin' in plan.get('databases',{}).get('tasks',{}).get('properties',{})
    if learning:
        active = active.replace('\"Scope\" != \"Optional\"', '(\"Scope\" != \"Optional\" OR \"Planning Origin\" = \"Teacher recommendation\")')
        active += 'FILTER \"Preparation Status\" IS EMPTY OR \"Preparation Status\" IN (\"Active\", \"Needs confirmation\") OR \"Planning Origin\" = \"Teacher requirement\"; '
    current_resources=('FILTER "Current" = TRUE AND "Scope" != "Historical"; '
                       if 'Current' in plan.get('databases',{}).get('resources',{}).get('properties',{}) else '')
    current_announcements=('FILTER "Scope" != "Historical"; '
                           if 'Scope' in plan.get('databases',{}).get('announcements',{}).get('properties',{}) else '')
    add('root:courses',root,'courses','本学期课程','gallery',tf+'SORT BY "Course Code" ASC; SHOW "Name", "Course Code", "Term"')
    study_column='"Study Date", ' if scoped else ''
    add('root:tasks',root,'tasks','未完成任务','table',tf+active+'FILTER "Done" = FALSE; SORT BY "Due" ASC; SHOW "Name", "Course", "Source Space", "Due", '+study_column+'"Planned", "Done", "Priority", "Canvas Status"')
    add('root:undated',root,'tasks','无提交截止的准备与待确认事项','table',tf+active+'FILTER "Due" IS EMPTY AND "Done" = FALSE; SHOW "Name", "Course", "Source Space", '+study_column+'"Done"')
    if scoped:
        add('root:optional',root,'tasks','可选机会与条件性事项','table',tf+'FILTER "Scope" = "Optional"; SORT BY "Due" ASC; SHOW "Name", "Course", "Due", "Done"')
        add('root:history',archive,'tasks','其他学期或学校历史','table','FILTER "Scope" = "Historical"; SHOW "Name", "Source Space", "Due", "Done"')
        add('root:reference',archive,'tasks','分组信息与参考记录','table',tf+'FILTER "Scope" = "Reference"; SHOW "Name", "Course", "Source URL"')
        add('root:completed',archive,'tasks','已完成与已提交','table',tf+'FILTER "Done" = TRUE OR ("Canvas Status" IN ("graded", "submitted", "pending_review") AND "Evidence Status" != "partial_completed") OR "Evidence Status" = "confirmed_completed"; SHOW "Name", "Course", "Done", "Canvas Status", "Evidence Status"')
    if learning:
        add('root:learning',root,'tasks','按课节学习安排','table',tf+active+'FILTER \"Done\" = FALSE AND \"Learning Activity\" IS NOT EMPTY; SORT BY \"Study Date\" ASC; SHOW \"Name\", \"Course\", \"Learning Activity\", \"Planning Origin\", \"Study Date\", \"Prepare Before\", \"Planned\", \"Done\"')
    add('root:weeks',root,'weeks','每周学习目标','table',tf+'SORT BY "Start" DESC; SHOW "Name", "Start", "Status", "Available Minutes", "Scheduled Minutes"')
    add('root:sessions',root,'sessions','学习记录','table',tf+'SORT BY "Started" DESC; SHOW "Name", "Task", "Week", "Started", "Minutes", "Status"')
    add('root:deadlines',root,'tasks','任务截止日历','calendar',tf+active+'FILTER "Done" = FALSE AND "Due Conflict" = FALSE; CALENDAR BY "Due"; SHOW "Name", "Course", "Done"')
    add('root:announcements',root,'announcements','通知汇总','table',tf+current_announcements+'SORT BY "Posted" DESC; SHOW "Name", "Course", "Source Space", "Posted", "Has Action Items"')
    add('root:syllabus',root,'resources','Syllabus 与 Course Outline','table',tf+current_resources+'FILTER "Syllabus" = TRUE; SHOW "Name", "Course", "Source URL", "Download Status"')
    add('root:class-notes',root,'notes','课堂笔记','table',tf+'FILTER "Type" = "Class notes"; SHOW "Name", "Course", "Storage", "Last Indexed", "Link Status", "Obsidian URI"')
    add('root:resources',root,'resources','跨课程资料索引','table',tf+current_resources+'GROUP BY "Course"; SHOW "Name", "Course", "Storage", "Last Indexed", "Link Status", "Obsidian URI", "Source URL", "Download Status"')
    if current_resources:
        add('root:resource-history',archive,'resources','资料历史版本','table',tf+'FILTER "Current" = FALSE; GROUP BY "Course"; SHOW "Name", "Course", "Version", "Local Path"')
    add('root:timetable',root,'timetable','课程日程与活动','calendar',tf+'CALENDAR BY "Start"; SHOW "Name", "Course", "Source Space", "Start", "End"')
    for record in plan.get('records',[]):
        if record['kind']!='courses':
            continue
        source=record['source_key'];page=state.get('records',{}).get(source,{}).get('page_id')
        if not page:
            continue
        cf=tf+f'FILTER "Course" = {json.dumps(page)}; '
        add(source+':tasks',page,'tasks','Assignments 与课程待办','table',cf+'SORT BY "Due" ASC; SHOW "Name", "Due", "Planned", "Done", "Canvas Status"')
        add(source+':resources',page,'resources','课程资料','table',cf+current_resources+'SHOW "Name", "Type", "Source URL", "Download Status"')
        add(source+':notes',page,'notes','Notes 与重要信息','table',cf+'SHOW "Name", "Type", "Storage", "Last Indexed", "Link Status", "Personal Notes"')
        add(source+':announcements',page,'announcements','课程通知','table',cf+current_announcements+'SORT BY "Posted" DESC; SHOW "Name", "Posted", "Has Action Items"')
        add(source+':timetable',page,'timetable','课程日程','calendar',cf+'CALENDAR BY "Start"; SHOW "Name", "Start", "End"')
    return views
