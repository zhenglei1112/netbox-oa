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
        "module_width": 0.375,
        "module_height": 6.75,
        "quiet_zone": 3,
        "font_size": 0,
        "write_text": False,
    })
    # Keep the reference's evenly spaced caption inside the SVG so printing
    # scales the bars and their human-readable number together.
    svg = ElementTree.fromstring(barcode)
    width = float(svg.attrib["width"].removesuffix("mm"))
    svg.set("height", "14.5mm")
    caption = f"*{number}*"
    caption_width = (width - 6) * 0.92
    start = (width - caption_width) / 2
    text = ElementTree.SubElement(svg, "{http://www.w3.org/2000/svg}text", {
        "x": " ".join(
            f"{start + index * caption_width / (len(caption) - 1):.3f}mm"
            for index in range(len(caption))
        ),
        "y": "12.25mm",
        "text-anchor": "middle",
        "font-family": "Times New Roman, serif",
        "font-size": "12pt",
        "fill": "black",
    })
    text.text = caption
    barcode = ElementTree.tostring(svg, encoding="utf-8")
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=4)
    qr.add_data(verification_url)
    qr.make(fit=True)
    return {
        "order_barcode": svg_data_uri(barcode),
        "barcode_width": svg.attrib["width"],
        "verification_qr": svg_data_uri(qr.make_image(image_factory=SvgPathImage).to_string()),
        "verification_url": verification_url,
    }
