import sys, unittest, tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'skill/canvas-notion-study/scripts'))
from study_sync.state import *

class StateTests(unittest.TestCase):
    def test_local_path_keeps_literal_underscores_and_raw_receipt_value(self):
        raw='/materials/ESC194_Tutorial_1_-_Pre_Tutorial_-2.pdf'
        record={'kind':'resources','source_key':'file:1','properties':{'Name':'Tutorial','Local Path':raw},'generated_content':'Original file'}
        plan={'records':[record]};state=empty_state();bindings={'resources':{'data_source_id':'resources'}}
        op=prepare_operations(plan,state,bindings)['operations'][0]
        self.assertEqual(op['properties']['Local Path'],raw.replace('_',r'\_'))
        self.assertEqual(op['source_properties']['Local Path'],raw)
        self.assertEqual(record['properties']['Local Path'],raw)
        begin_operations(state,[op])
        commit_receipts(state,[{**op,'status':'succeeded','page_id':'page1'}])
        self.assertEqual(state['records']['file:1']['properties']['Local Path'],raw)
        self.assertEqual(prepare_operations(plan,state,bindings)['operations'],[])

    def test_week_links_resolve_only_to_existing_pages(self):
        record={'kind':'weeks','source_key':'week:1','properties':{'Name':'Week'},'generated_content':'开始 [[record:task:quiz]]'}
        plan={'records':[record]};state=empty_state();binding={'weeks':{'data_source_id':'weeks'}}
        self.assertEqual(prepare_operations(plan,state,binding)['blocked'][0]['reason'],'unresolved_relations')
        state['records']['task:quiz']={'page_id':'12345678-1111-2222-3333-444444444444'}
        op=prepare_operations(plan,state,binding)['operations'][0]
        self.assertIn('<mention-page url="https://app.notion.com/p/12345678111122223333444444444444"/>',op['content'])
        self.assertNotIn('[[record:',op['content'])
    def test_schema_relations_resolve_mentor_ids_without_mapping_multiselect(self):
        task='gmail:message:abc:action:survey'
        week='mentor:instance:week:2026-09-14'
        plan={'databases':{'weeks':{'properties':{'Tasks':{'type':'relation'},'Tags':{'type':'multi_select'}}}},'records':[{'kind':'weeks','source_key':week,'properties':{'Name':'Week','Tasks':[task],'Tags':[task]},'generated_content':'Week plan'}]}
        state=empty_state()
        binding={'weeks':{'data_source_id':'weeks-source'}}
        self.assertEqual(prepare_operations(plan,state,binding)['blocked'][0]['reason'],'unresolved_relations')
        state['records'][task]={'page_id':'task-page'}
        props=prepare_operations(plan,state,binding)['operations'][0]['properties']
        self.assertEqual(props['Tasks'],['task-page'])
        self.assertEqual(props['Tags'],[task])
    def test_inflight_update_blocks_even_if_plan_returns_to_old_fingerprint(self):
        plan=self.fixture();state=empty_state();binding={'tasks':{'data_source_id':'ds'}}
        op=prepare_operations(plan,state,binding)['operations'][0];begin_operations(state,[op]);commit_receipts(state,[{**op,'status':'succeeded','page_id':'p'}])
        old_content=plan['records'][0]['generated_content']
        plan['records'][0]['generated_content']='Possibly applied update'
        pending=prepare_operations(plan,state,binding)['operations'][0];begin_operations(state,[pending])
        plan['records'][0]['generated_content']=old_content
        result=prepare_operations(plan,state,binding)
        self.assertEqual(result['unchanged'],0)
        self.assertEqual(result['blocked'][0]['reason'],'inflight_requires_reconciliation')
    def fixture(self):
        return {"records":[{"kind":"tasks","source_key":"canvas:host:user:1:assignment:2","properties":{"Name":"Essay","Source Key":"canvas:host:user:1:assignment:2","Done":"__NO__"},"generated_content":"Due as published."}]}
    def test_confirmed_create_repeated_plan_has_no_writes(self):
        plan=self.fixture(); state=empty_state(); binding={"tasks":{"data_source_id":"ds"}}
        batch=prepare_operations(plan,state,binding)
        self.assertNotIn('Done',batch['operations'][0]['properties'])
        op=batch['operations'][0]; begin_operations(state,[op])
        commit_receipts(state,[{"source_key":op['source_key'],"operation_id":op['operation_id'],"status":"succeeded","page_id":"page1"}])
        self.assertEqual(prepare_operations(plan,state,binding)['operations'],[])
    def test_timeout_must_reconcile_before_retry(self):
        state=empty_state(); plan=self.fixture(); binding={"tasks":{"data_source_id":"ds"}}
        op=prepare_operations(plan,state,binding)['operations'][0];begin_operations(state,[op])
        commit_receipts(state,[{"source_key":op['source_key'],"operation_id":op['operation_id'],"status":"unknown"}])
        batch=prepare_operations(plan,state,binding)
        self.assertEqual(batch['operations'],[])
        self.assertEqual(batch['blocked'][0]['reason'],'inflight_requires_reconciliation')
    def test_changed_due_is_update_same_page(self):
        plan=self.fixture();state=empty_state();binding={"tasks":{"data_source_id":"ds"}}
        op=prepare_operations(plan,state,binding)['operations'][0];begin_operations(state,[op]);commit_receipts(state,[{**op,'status':'succeeded','page_id':'p'}])
        plan['records'][0]['properties']['date:Due:start']='2026-09-20'
        changed=prepare_operations(plan,state,binding)['operations'][0]
        self.assertEqual(changed['action'],'update');self.assertEqual(changed['page_id'],'p')
    def test_managed_edit_preserves_personal_and_linked_views(self):
        body=managed_page('Old source')+'\n\nMy private handwritten note\n<database url="https://notion.so/example">Tasks</database>'
        change=content_edit(body,'New source','Old source')
        updated=body.replace(change['old_str'],change['new_str'])
        self.assertIn('New source',updated);self.assertIn('My private handwritten note',updated);self.assertIn('<database',updated)
    def test_managed_user_edit_detected(self):
        with self.assertRaises(ValueError):content_edit(managed_page('User changed source'),'New source','Old source')
    def test_atomic_json_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'state.json';write_json(p,{'unicode':'课程'});self.assertEqual(read_json(p),{'unicode':'课程'})
    def test_mismatched_receipt_cannot_commit(self):
        with self.assertRaises(ValueError):commit_receipts(empty_state(),[{'source_key':'unknown','operation_id':'x','status':'succeeded','page_id':'p'}])
    def test_removed_official_date_is_explicitly_cleared(self):
        plan=self.fixture();state=empty_state();binding={'tasks':{'data_source_id':'ds'}}
        plan['records'][0]['properties'].update({'date:Due:start':'2026-09-20','date:Due:is_datetime':0})
        op=prepare_operations(plan,state,binding)['operations'][0];begin_operations(state,[op]);commit_receipts(state,[{**op,'status':'succeeded','page_id':'p'}])
        del plan['records'][0]['properties']['date:Due:start'];del plan['records'][0]['properties']['date:Due:is_datetime']
        changed=prepare_operations(plan,state,binding)['operations'][0]
        self.assertEqual(changed['properties']['date:Due:start'],'')
        self.assertEqual(changed['properties']['date:Due:is_datetime'],0)
    def test_flattened_personal_dates_never_written(self):
        plan=self.fixture();plan['records'][0]['properties'].update({'date:Planned:start':'2026-09-13','date:Planned:is_datetime':0})
        op=prepare_operations(plan,empty_state(),{'tasks':{'data_source_id':'ds'}})['operations'][0]
        self.assertNotIn('date:Planned:start',op['properties']);self.assertNotIn('date:Planned:is_datetime',op['properties'])
    def test_structured_projection_content_is_readable(self):
        record={'kind':'announcements','source_key':'test','properties':{'Name':'Notice'},'generated_content':{'summary':'老师发布了更新。','action_items':['阅读通知']}}
        op=prepare_operations({'records':[record]},empty_state(),{'announcements':{'data_source_id':'ds'}})['operations'][0]
        self.assertIn('老师发布了更新。',op['content']);self.assertNotIn('"summary"',op['content'])

if __name__=='__main__':unittest.main()
