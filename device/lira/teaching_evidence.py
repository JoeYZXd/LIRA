"""Bounded local transcript/reference alignment, never an automatic grade."""
import asyncio
import difflib
import unicodedata


class TeachingEvidence:
    MAX_TEXT=8000
    def __init__(self,memory,content,delivered_speech):
        self.memory,self.content,self.delivered_speech=memory,content,delivered_speech

    @staticmethod
    def _normalize(text):
        return ''.join(c for c in text if not c.isspace() and not unicodedata.category(c).startswith('P'))

    @classmethod
    def _align(cls,reference,observed):
        a,b=cls._normalize(reference),cls._normalize(observed)
        # autojunk bounds repeated-text behavior; limits keep worst-case CPU bounded.
        return [{'kind':op,'reference_span':[i,j],'observed_span':[k,l],
                 'reference':a[i:j],'observed':b[k:l]}
                for op,i,j,k,l in difflib.SequenceMatcher(None,a,b,autojunk=True).get_opcodes() if op!='equal'][:200]

    async def compare(self,args):
        memory=self.memory;mid=memory.owner()
        if mid is None:return {'status':'unavailable'}
        turn=memory.source(args.get('turn_id'))
        if not turn:return {'status':'needs_input'}
        reference=self.content.read(args['content_id'],version=args['version'],limit=self.MAX_TEXT)
        if reference['status']!='ok':return reference
        if reference['total_length']>self.MAX_TEXT:return {'status':'unavailable','reason':'reference_exceeds_alignment_limit','max_characters':self.MAX_TEXT}
        stamp=memory.stamp()
        differences=await asyncio.to_thread(self._align,reference['text'],turn['text'])
        if memory.stamp()!=stamp or memory.source(turn['id'])!=turn:return {'status':'conflict'}
        # Recheck content after thread yield: a deleted/replaced version cannot be silently used.
        if self.content.read(args['content_id'],version=args['version'],limit=1)['status']!='ok':return {'status':'version_conflict'}
        spoken=[dict(e) for e in self.delivered_speech() if e.get('generation')==turn['generation'] and e.get('owner')==turn['owner']
                and isinstance(e.get('delivered_at'),(int,float)) and e['delivered_at']<=turn['observed_at']]
        evidence={'reference_id':args['content_id'],'reference_version':reference['version'],'reference_source':reference['source'],
            'reference_text':reference['text'],'observed_transcript':turn['text'],'observed_turn_id':turn['id'],
            'differences':differences,'alignment_normalization':'whitespace_and_punctuation_removed',
            'state':'needs_review','uncertainty':['ASR may omit, repeat or misrecognize speech.','A turn does not prove a complete recitation.'],
            'delivered_speech':spoken,'assistance_tracking_scope':'exact_speech_success_only; ordinary speech and semantic hint relationship unknown',
            'hint_usage':'unknown','mastery':'unknown'}
        row=memory._put(mid,'recitation_evidence',evidence,'observed_asr_alignment',f"{turn['id']}:{args['content_id']}:{args['version']}",content_id=args['content_id'])
        if row.get('status')=='conflict':return row
        return {'status':'ok','review_state':'needs_review','id':row['id'],'evidence':row['body'],'data_only':True}

    def register(self,registry):
        registry.register('compare_observed_recitation','Align actual locally observed current ASR turn to a human-confirmed versioned text/poem. Evidence only: ASR uncertainty and completeness always need review, no automatic correct/mastery grade. Cannot submit transcript or hint flags.',
            {'type':'object','properties':{'content_id':{'type':'string','maxLength':100},'version':{'type':'integer','minimum':1},'turn_id':{'type':'string','maxLength':40}},
             'required':['content_id','version','turn_id'],'additionalProperties':False},self.compare,sensitive=True)
