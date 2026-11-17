"""Read synthetic lead tokens from one verified captured form page."""
from __future__ import annotations
import json
from html.parser import HTMLParser
from pathlib import Path
from .storage import Storage
from .util import sha256_bytes


class _LeadParser(HTMLParser):
    def __init__(self):
        super().__init__();self.leads=[]
    def handle_starttag(self,tag,attrs):
        if tag!='article':return
        row=dict(attrs)
        if 'data-lead-id' not in row:return
        keys=['data-lead-id','data-tenant','data-email-token','data-phone-token','data-account-token','data-consent']
        if any(not row.get(k) for k in keys):raise ValueError('lead form missing fields')
        self.leads.append({'lead_id':row['data-lead-id'],'tenant':row['data-tenant'],
                           'email_token':row['data-email-token'],
                           'phone_token':row['data-phone-token'],
                           'account_token':row['data-account-token'],
                           'consent':row['data-consent'],'sale_id':row.get('data-sale-id','')})


def verify_lead_intake(storage:Storage,run_id:str,body_path:str|Path,
                       intake_path:str|Path)->dict:
    body=Path(body_path).read_bytes()
    config=json.loads(Path(intake_path).read_text(encoding='utf-8'))
    matches=[e for e in storage.get_request_events(run_id)
             if e['url']==config['source_url'] and e['outcome']=='success'
             and e['body_sha256']==sha256_bytes(body)
             and int(e['response_bytes'])==len(body)
             and str(e['content_type']).startswith('text/html')]
    if len(matches)!=1:raise ValueError('exactly one matching successful HTML receipt required')
    parser=_LeadParser();parser.feed(body.decode('utf-8'))
    ids=[r['lead_id'] for r in parser.leads]
    if not ids or len(ids)!=len(set(ids)):raise ValueError('lead IDs invalid')
    return {'run_id':run_id,'body_sha256':matches[0]['body_sha256'],
            'site_id':matches[0]['site_id'],'leads':parser.leads,
            'cannot_link':config['cannot_link'],'contracts':config['contracts']}


def preview_token_components(intake:dict)->list[list[str]]:
    """Naively merge any shared token, without tenant or contradiction rules."""
    parent={r['lead_id']:r['lead_id'] for r in intake['leads']}
    def root(x):
        while parent[x]!=x:x=parent[x]
        return x
    for i,left in enumerate(intake['leads']):
        for right in intake['leads'][i+1:]:
            if left['email_token']==right['email_token'] or left['phone_token']==right['phone_token'] or left['account_token']==right['account_token']:
                parent[root(right['lead_id'])]=root(left['lead_id'])
    groups={}
    for row in intake['leads']:
        groups.setdefault(root(row['lead_id']),[]).append(row['lead_id'])
    return sorted([sorted(v) for v in groups.values()])
