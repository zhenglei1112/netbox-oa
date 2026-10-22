"""Local rendered material-review fixture for the optional PDF browser regression."""

import os
import re
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace as NS
from urllib.parse import urlsplit

import django
from django.template.loader import render_to_string
from django.test import RequestFactory
from PIL import Image

WORKSPACE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WORKSPACE))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.settings")

django.setup()

root = Path(sys.argv[1]).resolve()
root.mkdir(parents=True, exist_ok=True)
Image.new("RGB", (700, 990), "white").save(root / "sample.png")
groups, items = [], []
for key, name, documents in [
    ("order", "维护服务工单", [(1, "scan", "pdf"), (2, "form", "form")]),
    ("report", "服务报告", [(3, "scan", "pdf"), (4, "electronic", "image")]),
]:
    rows = []
    group = {
        "key": key,
        "name": name,
        "item_count": len(documents),
        "can_upload": False,
        "index_rows": rows,
    }
    groups.append(group)
    for pk, kind, file_type in documents:
        document = NS(
            pk=pk,
            kind=kind,
            is_sealed=False,
            version=1,
            short_name="sample.pdf",
            original_name="sample.pdf",
            source_document=None,
            replaces_document_id=None,
        )
        rows.append(
            {
                "label": f"第 {len(rows) + 1} 份",
                "files": [
                    {
                        "document": document,
                        "label": "审批件" if kind == "scan" else "电子版",
                        "icon": "mdi-file-document-outline",
                    }
                ],
                "default_document": document,
            }
        )
        items.append(
            {
                "document": document,
                "group": group,
                "label": f"{name} · 第 {len(rows)} 份",
                "type": file_type,
                "item_id": pk,
                "preferred": True,
            }
        )
groups[0]["index_rows"][0]["files"].extend(groups[0]["index_rows"].pop()["files"])
groups[0]["item_count"] = 1
items[1]["item_id"] = 1
items[1]["preferred"] = False
html = render_to_string(
    "netbox_oa/maintenance/review.html",
    {
        "request": RequestFactory().get("/review/?embed=1"),
        "order": NS(number="Y-TEST", get_absolute_url="/"),
        "groups": groups,
        "selected_group": groups[0],
        "selected": items[0]["document"],
        "review_items": items,
    },
)
html = re.sub(r'data-source="[^"]*/materials/\d+/preview/"', 'data-source="/sample.pdf"', html)
html = re.sub(r'data-source="[^"]*/materials/2/print/\?embed=1"', 'data-source="/form.html"', html)
html = html.replace(
    'data-type="image" data-title="服务报告 · 第 2 份" data-source="/sample.pdf"',
    'data-type="image" data-title="服务报告 · 第 2 份" data-source="/sample.png"',
)
(root / "index.html").write_text(html, encoding="utf-8")
(root / "form.html").write_text(
    '<html><body style="margin:0"><main class="print-sheet" '
    'style="height:1400px;background:white;padding:30px">'
    "<h1>Electronic form fixture</h1></main></body></html>",
    encoding="utf-8",
)


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def translate_path(self, path):
        path = urlsplit(path).path
        if path.startswith("/static/"):
            return str(WORKSPACE / "netbox_oa/static" / path.removeprefix("/static/"))
        return str(root / path.lstrip("/")) if path != "/" else str(root / "index.html")

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
        elif path in ["/static/netbox.css", "/static/netbox-external.css"]:
            self.send_response(200)
            self.send_header("Content-Type", "text/css")
            self.end_headers()
            self.wfile.write(
                b":root{--bs-border-color:#ddd;--bs-link-color:#008080;"
                b"--bs-primary-bg-subtle:#d5eeee;--bs-secondary-color:#667;"
                b"--bs-primary:#008080;--bs-body-bg:white;--bs-body-color:#223;"
                b"--bs-tertiary-bg:#eef1f5}.btn{display:inline-flex;align-items:center;"
                b"padding:8px 12px;border-radius:4px}.btn-primary{background:#008080;color:white}"
                b".btn-ghost-primary{color:#008080}"
            )
        else:
            super().do_GET()


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
print(f"Review fixture: http://127.0.0.1:{server.server_port}", flush=True)
server.serve_forever()
