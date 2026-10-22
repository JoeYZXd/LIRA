"""Freeze trusted textbook content; model proposals carry no review authority."""
import copy
import hashlib
import re
import uuid
from .dictation_task_contract import canonical_json, content_hash, validate_task

def choose_words(targets, candidates, history):
    remaining=set(targets); chosen=[]
    candidates=list(dict.fromkeys(w for w in candidates if isinstance(w,str) and re.fullmatch(r'[\u3400-\u9fff]{1,32}',w)))
    while remaining:
        ranked=sorted(candidates,key=lambda w:(-len(remaining.intersection(w)),history.count(w),len(w)==1,candidates.index(w)))
        if not ranked or not remaining.intersection(ranked[0]):raise ValueError('word coverage incomplete: '+''.join(sorted(remaining)))
        word=ranked[0];chosen.append(word);remaining.difference_update(word);candidates.remove(word)
    return chosen

def asset_id(text):
    return 'speech.'+hashlib.sha256(text.encode('utf8')).hexdigest()[:40]

class DictationPreparation:
    def __init__(self,textbooks,settings,repository):
        self.textbooks,self.settings,self.repository=textbooks,settings,repository

    def prepare(self,args,*,callback_registration_id,owner_id=None):
        books=copy.deepcopy([b for b in self.textbooks.available_books() if b.get('subject','chinese') in ('chinese','语文')])
        request={k:v for k,v in args.items() if k in ('book_id','scope_type','start','end','unit','character_group')}
        request.setdefault('character_group','all')
        # Reuse catalog scope semantics, but report every unreviewed selected lesson.
        book=next((b for b in books if b['id']==request.get('book_id')),books[0] if len(books)==1 and not request.get('book_id') else None)
        if not book:return {'status':'needs_input','missing_parameters':['book_id']}
        scope=request.get('scope_type');start=request.get('start');end=request.get('end',start)
        if scope not in ('lesson','lesson_range','unit') or type(start) is not int or type(end) is not int or end<start:return {'status':'needs_input','missing_parameters':['scope_type','start']}
        if scope!='lesson_range' and 'end' in request:return {'status':'needs_input','missing_parameters':['scope_type','end']}
        rows=sorted(book['lessons'],key=lambda r:(r['unit'],r['number']))
        selected=[r for r in rows if (r['unit']==start if scope=='unit' else start<=r['number']<=end and (request.get('unit') is None or r['unit']==request['unit']))]
        if not selected or scope!='unit' and ({r['number'] for r in selected}!=set(range(start,end+1)) or len(selected)!=len({r['number'] for r in selected})):return {'status':'scope_required','reason':'missing or ambiguous catalog lessons'}
        gaps=[dict(unit=r['unit'],number=r['number']) for r in selected if not r.get('reviewed',book['source']=='published')]
        if gaps:return {'status':'lesson_review_required','missing_lessons':gaps}
        config=self.settings.get()
        if config is None:return {'status':'configuration_required'}
        group=request['character_group']
        if group not in ('all','four','two'):raise ValueError('invalid character group')
        lessons=[];audio={'prompt.prepare':'准备开始默写。','prompt.finish':'默写时间到，默写完成。','prompt.pinyin':'请给指定的字标注拼音。'}
        provenance=[]
        for row in selected:
            proposal=args.get('proposals',{}).get(str(row['number']),{})
            four=list(dict.fromkeys(row['four'])) if group in ('all','four') else []
            two=list(dict.fromkeys(row['two'])) if group in ('all','two') else []
            history=self.repository.word_history(owner_id,book['id'],row['unit'],row['number']) if owner_id is not None else []
            past=[w['text'] for h in history for w in h['expanded_words']]
            expansion=row.get('expansions','')
            candidates=re.findall(r'[\u3400-\u9fff]{1,32}',expansion) if isinstance(expansion,str) else []
            candidates+=proposal.get('expanded_words',[])
            try:
                words=choose_words(four,candidates,past)
                recognition=choose_words(two,proposal.get('recognition_words',[]),[])
            except ValueError as exc:return {'status':'word_proposals_required','unit':row['unit'],'number':row['number'],'reason':str(exc)}
            required=proposal.get('pinyin_required_for',[])
            if not isinstance(required,list) or any(c not in four for c in required):raise ValueError('invalid pinyin targets')
            expanded=[]
            for word in words:
                aid=asset_id(word);audio[aid]=word
                covers=[c for c in four if c in word]
                expanded.append(dict(text=word,covers=covers,audio_asset_id=aid,pinyin_required_for=[c for c in required if c in covers]))
            rec=None
            if proposal.get('include_recitation',bool(row.get('text',''))):
                text=row.get('text','')
                if not text:return {'status':'recitation_text_required','number':row['number']}
                prompt='请背诵'+row['title']+'。';aid=asset_id(prompt);audio[aid]=prompt
                rec=dict(text=text,prompt_audio_asset_id=aid)
            raw=canonical_json(row);digest=hashlib.sha256(raw.encode('utf8')).hexdigest()
            revision=book.get('revision') or int(digest[:7],16)+1
            lesson_id=f"{book['id']}.unit-{row['unit']}.lesson-{row['number']}"
            provenance.append(dict(lesson_id=lesson_id,source_sha256=digest,selected_words=[dict(text=w,origin='reviewed_expansion' if w in expansion else 'model_proposal',reused=w in past) for w in words],recognition_origin='model_proposal'))
            focus=proposal.get('focus_targets',[])
            if proposal.get('focus_nasal'):
                from pypinyin import pinyin,Style
                focus=list(dict.fromkeys(focus+[c for c in four+two if any(re.search(r'(?:ang|eng|ing|ong|an|en|in|un|ün)$',v) for v in pinyin(c,style=Style.NORMAL,heteronym=True)[0])]))
            lessons.append(dict(lesson_id=lesson_id,source=dict(book_id=book['id'],unit=row['unit'],number=row['number'],revision=revision),four_targets=four,two_targets=two,expanded_words=expanded,recognition_words=[dict(text=w,covers=[c for c in two if c in w]) for w in recognition],recitation=rec,focus_targets=focus))
        task=dict(schema_version=1,task_id=uuid.uuid4().hex,content_hash='',config_revision=config['revision'],parameters=config['parameters'],callback_registration_id=callback_registration_id,lessons=lessons)
        task['content_hash']=content_hash(task)
        return dict(status='ready',task=validate_task(task),audio_texts=audio,provenance=provenance)
