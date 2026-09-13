import os, requests, collections, json

wh = os.environ.get('BITRIX_WEBHOOK') or os.environ.get('B24_WEBHOOK')
res = []
start = 0
while True:
    params = {
        'filter[STAGE_SEMANTIC_ID]': 'S',
        'filter[>CLOSEDATE]': '2026-02-20',
        'select[]': ['ID', 'LEAD_ID', 'CLOSEDATE', 'UF_CRM_1490965625', 'UF_CRM_1618371925',
                     'UF_CRM_58D8681AB22A4', 'UF_CRM_1492864724', 'UF_CRM_5ED460C45D3F2'],
        'start': start,
    }
    r = requests.get(wh + 'crm.deal.list', params=params, timeout=30).json()
    res += r['result']
    if 'next' not in r:
        break
    start = r['next']

c = collections.Counter()
for d in res:
    if d.get('UF_CRM_1490965625'): c['model'] += 1
    if d.get('UF_CRM_1618371925'): c['itog_cost'] += 1
    if d.get('UF_CRM_1492864724'): c['fix_pay'] += 1
    if d.get('UF_CRM_58D8681AB22A4'): c['year'] += 1

out = {
    'total': len(res),
    'coverage': dict(c),
    'itog_samples': [(d['ID'], d.get('UF_CRM_1490965625'), d.get('UF_CRM_1618371925')) for d in res if d.get('UF_CRM_1618371925')][:15],
    'deals': [{'id': d['ID'], 'lead_id': d.get('LEAD_ID'), 'closedate': d.get('CLOSEDATE'),
               'model': d.get('UF_CRM_1490965625'), 'cost': d.get('UF_CRM_1618371925'),
               'year': d.get('UF_CRM_58D8681AB22A4')} for d in res],
}
with open('/tmp/pricing_probe.json', 'w') as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
print('done', len(res), dict(c))
