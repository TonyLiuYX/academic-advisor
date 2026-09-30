import unittest, tempfile
from pathlib import Path
from study_sync.v1 import apply_preparations, refresh_mentor, build_v1_plan
from study_sync.projection import render_generated_content, tasks_to_ics
from study_sync.state import write_json, rendered_content


class V1IntegrationTests(unittest.TestCase):
    def test_rebuild_after_publishing_preserves_active_preparation_identity_and_fingerprint(self):
        snapshot={'canvas_origin':'https://canvas.example.edu','user':{'id':7},'generated_at':'2026-09-14T00:00:00Z',
                  'term':{'key':'2026-fall','label':'26fall','timezone':'America/Toronto'},
                  'courses':[{'id':101,'name':'Physics','course_code':'PHY101','mode':'course','html_url':'https://canvas.example.edu/courses/101'}]}
        config={'term':snapshot['term']}
        with tempfile.TemporaryDirectory() as directory:
            base=build_v1_plan(snapshot,{},config,{},directory)
            course=next(r['source_key'] for r in base['records'] if r['kind']=='courses')
            write_json(Path(directory)/'study-preparations.json',{'preparations':[{'id':'week2-reading','course_key':course,'title':'Read chapter 3',
                'study_date':'2026-09-14','source_refs':['https://canvas.example.edu/syllabus'],
                'study_steps':[{'id':'read','title':'Read and note one question','estimated_minutes':25}]}]})
            first=build_v1_plan(snapshot,{},config,{},directory)
            # A real receipt stores rendered text, so history restoration must
            # not replace a currently projected preparation with that receipt.
            state={'records':{r['source_key']:{'kind':r['kind'],'page_id':'page'+str(i),'properties':r['properties'],
                   'generated_content':rendered_content(r),'fingerprint':r['fingerprint']} for i,r in enumerate(first['records'])}}
            write_json(Path(directory)/'notion-state.json',state)
            second=build_v1_plan(snapshot,{},config,{},directory)
            self.assertEqual({r['source_key']:r['fingerprint'] for r in first['records']},
                             {r['source_key']:r['fingerprint'] for r in second['records']})
            prep=next(r for r in second['records'] if '|preparation=' in r['source_key'])
            self.assertEqual(prep['properties']['Scope'],'Academic')
            self.assertNotIn('date:Due:start',prep['properties'])
            withdrawal='https://canvas.example.edu/withdrawn-reading'
            write_json(Path(directory)/'record-dispositions.json',{'records':{prep['source_key']:{'scope':'Historical','source_refs':[withdrawal]}}})
            withdrawn=build_v1_plan(snapshot,{},config,{},directory)
            retired=next(r for r in withdrawn['records'] if r['source_key']==prep['source_key'])
            self.assertEqual(retired['properties']['Scope'],'Historical')
            self.assertIn(withdrawal,retired['source_refs'])
            self.assertIn(withdrawal,retired['properties']['Source'])

    def test_calendar_does_not_reactivate_history_or_turn_preparation_into_due(self):
        records=[{'kind':'tasks','source_key':scope,'properties':{'Name':scope,'Scope':scope,'date:Due:start':'2026-09-15'}}
                 for scope in ('Academic','Historical','Reference','Optional')]
        records.append({'kind':'tasks','source_key':'reading','properties':{'Name':'Reading','date:Study Date:start':'2026-09-15'}})
        calendar=tasks_to_ics({'records':records})
        self.assertEqual(calendar.count('BEGIN:VEVENT'),2)
        self.assertIn('SUMMARY:Academic',calendar)
        self.assertIn('SUMMARY:[可选] Optional',calendar)
        self.assertNotIn('SUMMARY:Historical',calendar)
        self.assertNotIn('SUMMARY:Reading',calendar)

    def test_class_preparation_stable_identity_and_separate_date(self):
        plan={'term':{'key':'term'},'databases':{},'stats':{},'records':[{'kind':'courses','source_key':'course:1','properties':{'Name':'Physics'}}]}
        payload={'preparations':[{'id':'2026-09-14:reading','course_key':'course:1','title':'Read sections 3.1–3.3','study_date':'2026-09-14','source_refs':['https://example.edu/syllabus'],
                                 'study_steps':[{'id':'reading','title':'Read the assigned sections','completion_criteria':'Write down one question'}]}]}
        requirements=apply_preparations(plan,payload)
        task=plan['records'][1]
        self.assertNotIn('date:Due:start',task['properties'])
        self.assertEqual(task['properties']['date:Study Date:start'],'2026-09-14')
        self.assertEqual(requirements[0]['task_keys'],[task['source_key']])
        apply_preparations(plan,payload)
        self.assertEqual(len(plan['records']),2)
        self.assertIn('Write down one question',render_generated_content(task))

    def test_preparation_extends_existing_task_instead_of_duplicate(self):
        plan={'term':{'key':'term'},'records':[{'kind':'courses','source_key':'course:1','properties':{}},{'kind':'tasks','source_key':'task:quiz','properties':{'Name':'Quiz','date:Due:start':'2026-09-15'},'generated_content':{}}]}
        payload={'preparations':[{'id':'quiz-prep','course_key':'course:1','canonical_task_key':'task:quiz','title':'Quiz preparation','study_date':'2026-09-14','source_refs':['https://example.edu/quiz'],'study_steps':[{'id':'examples','title':'Review examples'}]}]}
        apply_preparations(plan,payload)
        self.assertEqual(len(plan['records']),2)
        self.assertEqual(plan['records'][1]['properties']['date:Due:start'],'2026-09-15')
        self.assertEqual(plan['records'][1]['study_steps'][0]['id'],'examples')

if __name__=='__main__':unittest.main()
