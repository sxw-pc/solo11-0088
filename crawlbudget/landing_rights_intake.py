"""Verify an offline captured synthetic ad landing page and preview one audit."""
from __future__ import annotations
import json
from html.parser import HTMLParser
from pathlib import Path
from .storage import Storage
from .util import sha256_bytes


class _CreativeParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.creatives=[]
    def handle_starttag(self,tag,attrs):
        if tag!='article':return
        fields=dict(attrs)
        if 'data-creative-id' not in fields:return
        required=['data-creative-id','data-rights','data-service-value']
        if any(not fields.get(k) for k in required):raise ValueError('creative missing fields')
        rights=fields['data-rights'].split(',')
        self.creatives.append({'creative_id':fields['data-creative-id'],
                               'requires':rights,'service_value':int(fields['data-service-value'])})


def verify_landing_intake(storage:Storage,run_id:str,body_path:str|Path,
                          intake_path:str|Path)->dict:
    body=Path(body_path).read_bytes()
    data=json.loads(Path(intake_path).read_text(encoding='utf-8'))
    matches=[e for e in storage.get_request_events(run_id)
             if e['url']==data['source_url'] and e['outcome']=='success'
             and e['body_sha256']==sha256_bytes(body)
             and int(e['response_bytes'])==len(body)
             and str(e['content_type']).startswith('text/html')]
    if len(matches)!=1:raise ValueError('exactly one matching successful HTML receipt required')
    parser=_CreativeParser();parser.feed(body.decode('utf-8'))
    ids=[r['creative_id'] for r in parser.creatives]
    if not ids or len(ids)!=len(set(ids)):raise ValueError('creative IDs invalid')
    rights=set(data['rights'])
    if any(not set(r['requires'])<=rights for r in parser.creatives):
        raise ValueError('creative references unknown right')
    return {'run_id':run_id,'site_id':matches[0]['site_id'],
            'body_sha256':matches[0]['body_sha256'],
            'source_url':data['source_url'],'creatives':parser.creatives,
            'rights':data['rights'],'possible_worlds':data['possible_worlds'],
            'contracts':data['contracts']}


def single_right_preview(intake:dict)->dict:
    score={r:sum(c['service_value'] for c in intake['creatives'] if r in c['requires'])
           for r in intake['rights']}
    first=min(score,key=lambda r:(-score[r],r))
    return {'first_audit':first,'mention_value':score[first],
            'adaptive_policy_computed':False}
