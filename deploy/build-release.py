"""Package the tested build without local records, raw data, credentials or node_modules."""
import json
from datetime import datetime
from pathlib import Path
import tarfile

root = Path(__file__).resolve().parents[1]
destination = root / ('deploy/cold-rolling-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '.tar.gz')
items = ['dist', 'package.json', 'package-lock.json', 'backend/app/capture.py',
         'backend/app/capture_ocr.py', 'backend/app/capture_tasks.py',
         'backend/app/capture_corrections.py',
         'backend/app/capture_storage.py', 'backend/app/capture_backup.py',
         'backend/requirements-capture-oss.txt', 'deploy/capture-cloud.env.example',
         'backend/requirements-capture.txt', 'deploy/server.mjs', 'deploy/install-ecs.sh',
         'deploy/setup-https.sh', 'deploy/nginx-http.conf', 'deploy/nginx-https.conf',
         'deploy/check-public.py', 'deploy/README.md', 'deploy/拍照数据库与云资源.md',
         'deploy/cold-rolling-backup.service', 'deploy/cold-rolling-backup.timer']
with tarfile.open(destination, 'w:gz') as archive:
    for item in items:
        archive.add(root / item, arcname=item, filter=lambda info: None if '/.wrangler/' in info.name else info)
print(json.dumps({'archive': str(destination), 'bytes': destination.stat().st_size}))
