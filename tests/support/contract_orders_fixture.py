"""Serve real OA views with isolated data and an installed NetBox source's UI assets."""

import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import BytesIO
from mimetypes import guess_type
from pathlib import Path
from urllib.parse import urlsplit

WORKSPACE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WORKSPACE))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.settings")

import django  # noqa: E402
from django.conf import settings  # noqa: E402

root = Path(sys.argv[1]).resolve()
netbox_root = Path(os.environ["NETBOX_SOURCE"]).resolve() / "netbox"
settings.DATABASES["default"]["NAME"] = str(root / "db.sqlite3")
settings.PLUGINS_CONFIG = {"netbox_oa": {"maintenance_storage_root": str(root / "private")}}
settings.TEMPLATES[0]["DIRS"].insert(0, str(netbox_root / "templates"))
django.setup()

from django.core.files.uploadedfile import SimpleUploadedFile  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.test import Client, RequestFactory  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

from netbox_oa import maintenance_services as services  # noqa: E402
from netbox_oa.models import MaintenanceOrder  # noqa: E402
from netbox_oa.template_content import ContractOrders  # noqa: E402
from netbox_oa.tests.test_contract_orders import ContractOrderTests  # noqa: E402

call_command("migrate", verbosity=0, interactive=False, run_syncdb=True)
fixture = ContractOrderTests()
fixture.setUp()
fixture.reach("report_fill")
for number in (1, 2):
    image = Image.new("RGB", (700, 990), "white")
    ImageDraw.Draw(image).text((40, 50), f"Service report {number}", fill="black")
    content = BytesIO()
    image.save(content, format="PNG")
    services.upload_document(
        pk=fixture.order.pk, actor=fixture.actor, material="report", state="scan",
        file=SimpleUploadedFile(f"report-{number}.png", content.getvalue(), "image/png"),
    )
for index in range(26):
    copy = MaintenanceOrder.objects.get(pk=fixture.order.pk)
    copy.pk = None
    copy.number = f"BROWSER-{index:02}"
    copy.save()
client = Client()
client.force_login(fixture.actor)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if path.startswith("/static/"):
            relative = path.removeprefix("/static/")
            base = (WORKSPACE / "netbox_oa/static" if relative.startswith("netbox_oa/")
                    else netbox_root / "project-static/dist")
            asset = (base / relative).resolve()
            if not asset.is_relative_to(base.resolve()) or not asset.is_file():
                self.send_error(404)
                return
            self.send_response(200)
            content_type = guess_type(str(asset))[0] or "application/octet-stream"
            self.send_header("Content-Type", content_type)
            self.end_headers()
            self.wfile.write(asset.read_bytes())
            return
        if path == "/":
            request = RequestFactory().get(self.path)
            request.user = fixture.actor
            table = ContractOrders({"request": request, "object": fixture.contract}).right_page()
            html = (
                '<!doctype html><html lang="zh-CN" data-bs-theme="light"><head>'
                '<meta charset="utf-8"><meta name="viewport" content="width=device-width">'
                '<link rel="stylesheet" href="/static/netbox-external.css">'
                '<link rel="stylesheet" href="/static/netbox.css"></head><body>'
                '<main class="p-3"><h1>外购合同</h1>'
                '<div class="row" id="payments"><div class="col col-md-12">'
                '<div class="card"><h5 class="card-header">付款</h5>'
                '<p class="p-3">合同付款列表</p></div></div></div>'
                + table + '</main></body></html>'
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode())
            return
        response = client.get(self.path)
        self.send_response(response.status_code)
        for key, value in response.items():
            self.send_header(key, value)
        self.end_headers()
        if response.streaming:
            for chunk in response.streaming_content:
                self.wfile.write(chunk)
        else:
            self.wfile.write(response.content)
        response.close()


server = HTTPServer(("127.0.0.1", 0), Handler)
print(f"Contract fixture: http://127.0.0.1:{server.server_port}", flush=True)
server.serve_forever()
