import copy,sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from context_api import public_today,Retired
from context_store import StoreError

class ReadingProjectionTests(unittest.TestCase):
    def fixture(self):
        return {'version':3,'date':'2026-01-01','timezone':'UTC','generated_at':'2026-01-01T12:00:00Z','revision':1,'retirement_generation':0,
            'overview':{'state':'ready','text':'先验证需求','is_partial':True,'covered_card_count':1,'pending_card_count':1,'coverage_start_at':'2026-01-01T10:00:00Z','coverage_end_at':'2026-01-01T10:01:00Z','updated_at':'2026-01-01T11:00:00Z'},
            'body':{'state':'unknown','lines':[],'sample_count':0,'measured_through_at':None},'coverage':[],
            'cards':[{'id':'a','version':1,'title':'想法','summary':'先验证需求','start_at':None,'end_at':None,'owner_label':'other','clean':'先验证需求','distilled':'先验证需求','reading_role':'main','summary_state':'ready','private_secret':'not-for-phone'}]}
    def test_ingestion_whitelist_and_bounds(self):
        value=self.fixture();value['ingestion']={'state':'budget_wait','latest_recorded_at':'2026-01-01T11:30:00Z','latest_transcribed_at':None,'latest_organized_at':None,'received_count':3,'transcribed_count':2,'queued_text_count':1,'queued_asr_count':1,'private_source_ids':['secret']}
        public=public_today(value,'2026-01-01',Retired({'group_ids':[],'generation':0}));self.assertNotIn('private_source_ids',public['ingestion'])
        value['ingestion']['queued_asr_count']=True
        with self.assertRaises(StoreError):public_today(value,'2026-01-01',Retired({'group_ids':[],'generation':0}))
    def test_optional_reading_fields_and_unknown_measurement(self):
        value=public_today(self.fixture(),'2026-01-01',Retired({'group_ids':[],'generation':0}))
        self.assertTrue(value['overview']['is_partial']);self.assertIsNone(value['body']['measured_through_at']);self.assertEqual(value['cards'][0]['reading_role'],'main');self.assertEqual(value['cards'][0]['owner_label'],'other');self.assertNotIn('private_secret',value['cards'][0])
    def test_invalid_optional_field_fails_closed(self):
        for field,value in [('covered_card_count',True),('coverage_end_at','no-zone'),('is_partial','yes')]:
            data=self.fixture();data['overview'][field]=value
            with self.assertRaises(StoreError):public_today(data,'2026-01-01',Retired({'group_ids':[],'generation':0}))
    def test_stale_generation_drops_old_coverage(self):
        data=public_today(self.fixture(),'2026-01-01',Retired({'group_ids':['deleted'],'generation':1}))
        self.assertEqual(data['overview']['text'],'');self.assertEqual(data['overview']['covered_card_count'],0);self.assertIsNone(data['overview']['coverage_end_at'])
