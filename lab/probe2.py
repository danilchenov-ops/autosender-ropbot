import os, requests, json
wh = os.environ.get('BITRIX_WEBHOOK') or os.environ.get('B24_WEBHOOK')
res=[]; start=0
SEL=['ID','LEAD_ID','CLOSEDATE','UF_CRM_1490965625','UF_CRM_1545669456','UF_CRM_590021190A0B7','UF_CRM_58FD45CBED9FA','UF_CRM_58D8681AB22A4','UF_CRM_1493265023']
while True:
    params={'filter[STAGE_SEMANTIC_ID]':'S','filter[>CLOSEDATE]':'2026-02-20','select[]':SEL,'start':start}
    r=requests.get(wh+'crm.deal.list',params=params,timeout=30).json()
    res+=r['result']
    if 'next' not in r: break
    start=r['next']
frame=[d for d in res if d.get('UF_CRM_1545669456')]
dogovor=[d for d in res if d.get('UF_CRM_590021190A0B7') or d.get('UF_CRM_58FD45CBED9FA')]
print('won:',len(res),'frame:',len(frame),'dogovor:',len(dogovor))
print('--- frame-filled samples (model | frame | contract#):')
for d in frame[:20]:
    print(d['ID'],'|',d.get('UF_CRM_1490965625'),'|',d.get('UF_CRM_1545669456'),'|',d.get('UF_CRM_590021190A0B7'))
