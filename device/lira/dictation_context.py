"""Read reviewed source rows and learner facts without inventing teaching decisions."""
import copy
from .dictation_task_contract import canonical_json
import hashlib

class DictationContext:
    def __init__(self,textbooks,results,profiles):
        self.textbooks,self.results,self.profiles=textbooks,results,profiles

    def resolve(self,args,owner_id,*,profile_snapshot=None,profile_pinned=False):
        books=copy.deepcopy([b for b in self.textbooks.available_books() if b.get('subject','chinese') in ('chinese','语文')])
        catalog=self.textbooks.catalog(books=books)
        profile=profile_snapshot if profile_pinned else self.profiles.get(owner_id)
        request={k:args[k] for k in ('book_id','scope_type','start','end','unit') if k in args}
        request.setdefault('book_id',profile['book_id'] if profile else books[0]['id'] if len(books)==1 else '')
        request['character_group']='all'
        if request.get('scope_type') not in ('lesson','lesson_range','unit') or type(request.get('start')) is not int:
            return dict(status='needs_input',missing_parameters=['scope_type','start'],catalog=catalog)
        checked=self.textbooks.prepare(request,books=books)
        if checked['status']!='ready':return dict(checked,catalog=catalog)
        book=next(b for b in books if b['id']==checked['book_id'])
        rows=[r for r in sorted(book['lessons'],key=lambda r:(r['unit'],r['number'])) if r['number'] in checked['lesson_numbers'] and (request.get('unit') is None or r['unit']==request['unit']) and (request['scope_type']!='unit' or r['unit']==request['start'])]
        lessons=[]
        for row in rows:
            digest=hashlib.sha256(canonical_json(row).encode()).hexdigest()
            facts=self.results.facts(owner_id,book['id'],row['unit'],row['number'])
            facts['word_history']=self.results.repository.word_history(owner_id,book['id'],row['unit'],row['number'])
            lessons.append(dict(lesson_id=f"{book['id']}.unit-{row['unit']}.lesson-{row['number']}",source=dict(book_id=book['id'],unit=row['unit'],number=row['number'],revision=book.get('revision') or int(digest[:7],16)+1),row=row,source_sha256=digest,facts=facts))
        return dict(status='ready',owner_id=owner_id,book_id=book['id'],book_title=book['title'],profile=profile,lessons=lessons,catalog=catalog)
