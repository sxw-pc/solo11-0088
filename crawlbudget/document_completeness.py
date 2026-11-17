"""Inspect sampled metadata in exactly captured synthetic documents."""
from __future__ import annotations
import json
from html.parser import HTMLParser
from pathlib import Path
from .quality_sampling import audit_sample
from .util import sha256_bytes


class _Meta(HTMLParser):
    def __init__(self):
        super().__init__()
        self.fields={}
    def handle_starttag(self,tag,attrs):
        if tag!='meta': return
        values=dict(attrs)
        if values.get('name') and values.get('content'):
            self.fields.setdefault(values['name'],[]).append(values['content'])


def inspect_documents(storage,run_id:str,frame:dict,intake_path:str|Path)->dict:
    path=Path(intake_path).resolve()
    data=json.loads(path.read_text(encoding='utf-8'))
    root=path.parents[2]
    audit=audit_sample(storage,run_id,frame)
    events=storage.get_request_events(run_id,site_id=frame['site_id'])
    rows=[]
    for obs in audit['observations']:
        url=obs['url']
        if url not in data['body_paths']:
            raise ValueError('sampled document body is not listed')
        body=(root/data['body_paths'][url]).read_bytes()
        matching=[event for event in events if event['url']==url and event['outcome']=='success'
                  and event['body_sha256']==sha256_bytes(body)
                  and int(event['response_bytes'])==len(body)
                  and str(event['content_type']).startswith('text/html')]
        if len(matching)!=1:
            raise ValueError('exactly one matching successful HTML receipt required')
        parser=_Meta()
        parser.feed(body.decode('utf-8'))
        fields={key:values[0] for key,values in parser.fields.items() if len(values)==1}
        rows.append({'url':url,'body_sha256':matching[0]['body_sha256'],
                     'present_fields':sorted(fields),
                     'full_metadata':all(key in fields for key in data['schema_fields']),
                     'public_index':all(key in fields for key in data['core_fields'])})
    return {'run_id':run_id,'site_id':frame['site_id'],'frame_size':audit['frame_size'],
            'sample_size':audit['sample_size'],'rows':rows,'population_certificate':None}
