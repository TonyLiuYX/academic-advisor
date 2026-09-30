import unittest
from study_sync.notion_schema import schema_updates

class SchemaTests(unittest.TestCase):
    def test_additive_mentor_fields_relations_and_text_alias(self):
        plan={'databases':{'tasks':{'properties':{'Source Key':{'type':'text'},'Scope':{'type':'select'},'Week':{'type':'relation','target':'weeks'}}}},'records':[{'kind':'tasks','properties':{'Scope':'Academic'}}]}
        result=schema_updates(plan,'tasks',{'schema':{'Source Key':{'type':'rich_text'}}},{'weeks':{'data_source_id':'week-source'}})
        self.assertEqual(result['changes'],2)
        self.assertIn('ADD COLUMN "Scope" SELECT(',result['statements'])
        self.assertIn('ADD COLUMN "Week" RELATION(\'week-source\')',result['statements'])
        self.assertNotIn('Done',result['statements'])
    def test_new_enum_preserves_existing_names_and_colors(self):
        plan={'databases':{'tasks':{'properties':{'Type':{'type':'select'}}}},'records':[{'kind':'tasks','properties':{'Type':'exam'}}]}
        remote={'schema':{'Type':{'type':'select','options':[{'name':'Custom','color':'red'},{'name':'assignment','color':'blue'}]}}}
        ddl=schema_updates(plan,'tasks',remote)['statements']
        self.assertIn("'Custom':red",ddl);self.assertIn("'assignment':blue",ddl);self.assertIn("'exam':default",ddl)
        remote['schema']['Type']['options'].append({'name':'exam','color':'green'})
        self.assertEqual(schema_updates(plan,'tasks',remote)['changes'],0)
    def test_enum_type_change_requires_reconciliation(self):
        plan={'databases':{'tasks':{'properties':{'Type':{'type':'select'}}}},'records':[]}
        with self.assertRaises(ValueError):schema_updates(plan,'tasks',{'Type':{'type':'text'}})
