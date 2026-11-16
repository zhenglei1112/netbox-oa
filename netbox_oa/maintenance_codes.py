"""Machine-readable identifiers for printed service orders."""

import base64
from xml.etree import ElementTree

import qrcode
from barcode import Code128
from barcode.writer import SVGWriter

QR_INK = "#001e2e"
QR_ACCENT = "#009e91"
SVG_NS = "http://www.w3.org/2000/svg"


def verification_qr_svg(verification_url):
    """Render the print brand in vector form, retaining a four-module quiet zone."""
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_H, border=4)
    qr.add_data(verification_url)
    qr.make(fit=True)
    matrix = qr.modules
    count = len(matrix)
    size = count + 8
    svg = ElementTree.Element(f"{{{SVG_NS}}}svg", {
        "viewBox": f"0 0 {size} {size}", "width": "26mm", "height": "26mm",
    })

    def rect(parent, x, y, width, height, fill, radius=0):
        return ElementTree.SubElement(parent, f"{{{SVG_NS}}}rect", {
            "x": f"{x:g}", "y": f"{y:g}", "width": f"{width:g}",
            "height": f"{height:g}", "fill": fill, "rx": f"{radius:g}",
        })

    rect(svg, 0, 0, size, size, "#fff")
    modules = ElementTree.SubElement(svg, f"{{{SVG_NS}}}g", {"transform": "translate(4 4)"})

    def finder(x, y):
        return ((x < 7 and y < 7) or (x >= count - 7 and y < 7)
                or (x < 7 and y >= count - 7))

    # Keep the three finder patterns square; round only vertical data runs.
    for x in range(count):
        y = 0
        while y < count:
            if not matrix[y][x]:
                y += 1
                continue
            start = y
            square = finder(x, y)
            y += 1
            if not square:
                while y < count and matrix[y][x] and not finder(x, y):
                    y += 1
            accent = not square and (x * 3 + start * 5) % 11 == 0
            rect(modules, x, start, 1, y - start, QR_ACCENT if accent else QR_INK,
                 0.45 if not square and y - start > 1 else 0)

    # White separation protects the logo at the small physical print size.
    logo_size = count * 0.23
    offset = (size - logo_size) / 2
    padding = logo_size * 0.12
    rect(svg, offset - padding, offset - padding,
         logo_size + 2 * padding, logo_size + 2 * padding, "#fff")
    logo = ElementTree.SubElement(svg, f"{{{SVG_NS}}}g", {
        "id": "company-logo",
        "transform": f"translate({offset:g} {offset:g}) scale({logo_size / 100:g})",
    })

    def bar(x, top, height, fill):
        # The reference has rounded tops and a shared, flat baseline.
        radius = 6
        ElementTree.SubElement(logo, f"{{{SVG_NS}}}path", {
            "fill": fill,
            "d": (f"M{x} {top + height}V{top + radius}"
                  f"a6 6 0 0 1 12 0V{top + height}Z"),
        })

    bar(0, 0, 100, QR_INK)
    for x, dark_height, green_height in ((22, 54, 28), (44, 39, 44), (66, 23, 60)):
        # Upper navy segments have rounded lower ends, as in the supplied mark.
        rect(logo, x, 0, 12, dark_height, QR_INK, 6)
        bar(x, 100 - green_height, green_height, QR_ACCENT)
    bar(88, 0, 100, QR_ACCENT)
    return ElementTree.tostring(svg, encoding="utf-8")


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
    return {
        "order_barcode": svg_data_uri(barcode),
        "barcode_width": svg.attrib["width"],
        "verification_qr": svg_data_uri(verification_qr_svg(verification_url)),
        "verification_url": verification_url,
    }
