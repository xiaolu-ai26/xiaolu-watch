import copy,hashlib,json,sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from text_worker import Worker
from overview_worker import Overview
from context_analysis.reading import cards

class WholeConversationSummaryTests(unittest.TestCase):
 def group(self,text,summary,start,continuation=False,identifier='g'):
  return {'id':identifier,'title':'换购问题','owner_caption':'归属未确认','clean_fragment':text,'distilled_fragment':'## 换购问题\n\n'+text,'summary_input':text,'continuation':continuation,'reading':{'role':'main','summary':summary},'source_spans':[{'source_id':'a'*64,'start':start,'end':start+2,'absolute_start':1792298100+start,'absolute_end':1792298102+start,'owner':'unknown','time_precision':'asr_word_span'}]}
 def batch(self,index,group):return {'id':'b'+str(index),'date':'2026-10-18','captured':1792298100+index*60,'session':'s','sequence':index,'sources':[],'output':{'clean':group['clean_fragment'],'distilled':group['distilled_fragment']},'conversation_groups':[group]}
 def test_multifragment_gets_one_cached_whole_summary_and_single_card_is_unchanged(self):
  class Provider:
   def __init__(self):self.calls=[]
   def _request(self,rows,policy,user,validator):
    scope=rows[0].identity()['summary_scope'];data=json.loads(user);self.calls.append((scope,data))
    out={'text':'询问如何换购，并补充已使用两年半。','source_ids':[x['id'] for x in data]};return validator(out),{'provider':'fixture'}
  with tempfile.TemporaryDirectory() as td:
   root=Path(td);provider=Provider();cfg={'version':1,'enabled':True,'home':str(root/'state'),'notes_outbox':str(root/'notes'),'reading_root':str(root/'reading'),'feishu':False};w=Worker(cfg,provider=provider)
   a=self.batch(0,self.group('怎么换购？用了多久？','询问换购和使用时长。',0));b=self.batch(1,self.group('用了两年半。','两年半。',60,True));single=self.batch(2,self.group('先验证需求。','先验证需求。',120,identifier='single'))
   for batch in (a,b,single):w.state.put('batch:'+batch['id'],batch)
   single_card=cards([single])[0];legacy={k:v for k,v in single_card.items() if k not in ('version','_fragment_count','_has_continuation')};self.assertEqual(single_card['version'],hashlib.sha256(json.dumps(legacy,sort_keys=True,ensure_ascii=False).encode()).hexdigest())
   overview=Overview(cfg,w);overview.tick(1792300000);projection=json.loads((root/'reading/2026-10-18.json').read_text());g=next(x for x in projection['cards'] if x['id']=='g');self.assertEqual(g['summary'],'询问如何换购，并补充已使用两年半。');self.assertNotEqual(g['summary'],'询问换购和使用时长。 两年半。');self.assertNotIn('_fragment_count',g)
   calls=[data for scope,data in provider.calls if scope=='conversation:g'];self.assertEqual(len(calls),1);self.assertIn('怎么换购？',calls[0][0]['text']);self.assertIn('用了两年半。',calls[0][0]['text']);self.assertFalse(any(scope=='card:single' for scope,_ in provider.calls))
   overview.tick(1792300060);self.assertEqual(sum(scope=='conversation:g' for scope,_ in provider.calls),1)
   changed=copy.deepcopy(b);changed['conversation_groups'][0]['summary_input']='用了三年。';changed['conversation_groups'][0]['clean_fragment']='用了三年。';w.state.put('batch:'+b['id'],changed);overview.tick(1792300120);self.assertEqual(sum(scope=='conversation:g' for scope,_ in provider.calls),2);w.state.close()

if __name__=='__main__':unittest.main()
