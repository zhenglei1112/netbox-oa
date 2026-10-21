"""Machine-readable identifiers for printed service orders."""

import base64
from xml.etree import ElementTree

import qrcode
from barcode import Code128
from barcode.writer import SVGWriter
from qrcode.image.svg import SvgPathImage


def svg_data_uri(value):
    return "data:image/svg+xml;base64," + base64.b64encode(value).decode("ascii")


def print_codes(number, verification_url):
    barcode = Code128(number, writer=SVGWriter()).render({
        "module_width": 0.25,
        "module_height": 5,
        "quiet_zone": 3,
        "font_size": 0,
        "write_text": False,
    })
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=4)
    qr.add_data(verification_url)
    qr.make(fit=True)
    return {
        "order_barcode": svg_data_uri(barcode),
        "barcode_width": ElementTree.fromstring(barcode).attrib["width"],
        "verification_qr": svg_data_uri(qr.make_image(image_factory=SvgPathImage).to_string()),
        "verification_url": verification_url,
    }
