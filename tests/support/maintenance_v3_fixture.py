"""Isolated HTTP fixture: real OA views, native NetBox templates/CSS, test permission adapters."""

import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from mimetypes import guess_type
from pathlib import Path
from urllib.parse import urlsplit

WORKSPACE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WORKSPACE))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.settings")

import django  # noqa: E402
from django.conf import settings  # noqa: E402

root = Path(sys.argv[1]).resolve()
native = Path(os.environ["NETBOX_SOURCE"]).resolve() / "netbox"
settings.DATABASES["default"]["NAME"] = str(root / "db.sqlite3")
settings.PLUGINS_CONFIG = {"netbox_oa": {"maintenance_storage_root": str(root / "private")}}
template_config = settings.TEMPLATES[0]
snapshots = {
    name: (native / "templates" / name).read_text(encoding="utf-8")
    for name in (
        "generic/object.html", "generic/object_list.html", "inc/table_controls_htmx.html",
        "inc/filter_list.html", "htmx/table.html", "inc/table_htmx.html", "inc/paginator.html",
        "inc/htmx_modal.html",
    )
}
snapshots["base/layout.html"] = '''<!doctype html><html lang="zh-CN" data-bs-theme="light">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<link rel="stylesheet" href="/static/netbox-external.css">
<link rel="stylesheet" href="/static/netbox.css"></head><body><main class="p-3">
{% for message in messages %}<p>{{ message }}</p>{% endfor %}
{% block header %}{% endblock %}{% block content %}{% endblock %}
</main>{% block modals %}{% endblock %}</body></html>'''
template_config["APP_DIRS"] = False
template_config["OPTIONS"]["loaders"] = [
    ("django.template.loaders.locmem.Loader", snapshots),
    "django.template.loaders.filesystem.Loader", "django.template.loaders.app_directories.Loader",
]
for library in ("helpers", "buttons", "plugins", "custom_links", "perms", "tabs"):
    template_config["OPTIONS"]["libraries"][library] = "tests.support.v3_native_tags"

django.setup()

from django.core.management import call_command  # noqa: E402
from django.test import Client  # noqa: E402

from netbox_oa.tests.test_maintenance_v3 import MaintenanceV3Tests  # noqa: E402

call_command("migrate", verbosity=0, interactive=False, run_syncdb=True)
fixture = MaintenanceV3Tests()
fixture.setUp()
fixture.reach("report_fill")
fixture.upload("report")
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
                    else native / "project-static/dist")
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
        self.respond(client.get(self.path))

    def do_POST(self):
        content = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.respond(client.generic(
            "POST", self.path, data=content,
            content_type=self.headers.get("Content-Type", "application/x-www-form-urlencoded"),
            HTTP_ACCEPT=self.headers.get("Accept", "text/html"),
        ))

    def respond(self, response):
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
print(f"V3 fixture: http://127.0.0.1:{server.server_port}", flush=True)
server.serve_forever()
