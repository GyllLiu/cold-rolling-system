"""Read-only public smoke checks. No production photo or pass record is created."""
import json
import re
import argparse
from urllib.error import HTTPError
from urllib.request import Request, urlopen

parser = argparse.ArgumentParser()
parser.add_argument('--public-capture', action='store_true', help='Verify explicitly enabled password-free capture access')
args = parser.parse_args()
origin = 'https://8.221.114.18'
for path, label in [('/', '工艺推演台'), ('/capture', '拍照记录')]:
    with urlopen(origin + path, timeout=30) as response:
        html = response.read().decode()
        assert response.status == 200 and label in html
        if path == '/capture':
            assert '当前道次' in html and len(re.findall(r'<button\b', html)) >= 8
            assert all(action in html for action in ('拍照', '开始', '结束'))
            # Material labels may appear as guidance or read-only OCR results;
            # the capture screen still has no manual material-entry form.
            assert not re.search(r'<select\b|<input\b[^>]*type="(?:text|number)"', html)
        asset = re.search(r'(?:src|href)="(/assets/[^" ]+)"', html)
    if asset:
        with urlopen(origin + asset.group(1), timeout=30) as response:
            assert response.status == 200
    print(path, 'page and assets OK')
payload = {'alloy':'1060','incoming_state':'annealed','entry_thickness':6,
           'target_thickness':0.7,'width':1280,'target_state':'h18'}
request = Request(origin+'/api/process-plan', data=json.dumps(payload).encode(),
                  headers={'Content-Type':'application/json', 'Origin':origin})
with urlopen(request, timeout=60) as response:
    result=json.load(response)
    assert response.status == 200
    print('recommendation API OK; response fields:', ', '.join(result))
with urlopen(origin+'/api/capture/health', timeout=20) as response:
    assert json.load(response)['status'] == 'ok'
for path in ['/api/capture/state','/api/capture/export.csv','/api/capture/history']:
    try:
        with urlopen(origin+path, timeout=20) as response:
            assert args.public_capture, 'Private endpoint exposed: '+path
            assert response.status == 200
            print(path, 'password-free access OK')
    except HTTPError as error:
        assert not args.public_capture, 'Password-free endpoint is still protected: '+path
        assert error.code == 401
        print(path, 'access code protection OK')
print('PUBLIC_SMOKE_OK')
