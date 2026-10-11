"""Layout the sequential definition as four SVG stage lanes."""

from textwrap import wrap


def build_diagram(stages):
    lane_width, node_width, node_height, spacing = 360, 260, 94, 136
    nodes, lanes, edges, returns = [], [], [], []
    height = 220 + max(len(stage["nodes"]) for stage in stages) * spacing
    for column, stage in enumerate(stages):
        lane_x = column * lane_width + 10
        lanes.append(
            {
                "x": lane_x,
                "title_x": lane_x + 20,
                "name": stage["name"],
                "count": len(stage["nodes"]),
            }
        )
        for row, node in enumerate(stage["nodes"]):
            x, y = lane_x + 80, 144 + row * spacing
            nodes.append(
                {
                    **node,
                    "x": x,
                    "y": y,
                    "cx": x + node_width / 2,
                    "bottom": y + node_height,
                    "middle": y + node_height / 2,
                    "label_lines": [
                        {"text": line, "y": y + 29 + index * 20}
                        for index, line in enumerate(wrap(node["name"], 17))
                    ],
                    "role_y": y + 78,
                    "number_x": x + 12,
                    "number_y": y + 16,
                }
            )
    by_key = {node["key"]: node for node in nodes}
    for index, node in enumerate(nodes):
        if index + 1 < len(nodes):
            target = nodes[index + 1]
            if node["stage"] == target["stage"]:
                path = f"M {node['cx']} {node['bottom']} V {target['y'] - 5}"
            else:
                corridor = target["x"] - 76
                path = (
                    f"M {node['cx']} {node['bottom']} V {node['bottom'] + 22} "
                    f"H {corridor} V 118 H {target['cx']} V {target['y'] - 5}"
                )
            edges.append({"source": node["key"], "target": target["key"], "path": path})
        if node["return_label"]:
            target = by_key[node["return_to"]]
            offset = sum(edge["stage"] == node["stage"] for edge in returns)
            corridor = node["x"] - 16 - offset * 8
            returns.append(
                {
                    "source": node["key"],
                    "target": target["key"],
                    "stage": node["stage"],
                    "label": f"{node['name']} → 退回 {target['name']}",
                    "path": (
                        f"M {node['x']} {node['middle']} H {corridor} "
                        f"V {target['middle']} H {target['x'] - 5}"
                    ),
                }
            )
    first, last = nodes[0], nodes[-1]
    edges.append(
        {
            "source": "start",
            "target": first["key"],
            "path": f"M {first['cx']} 118 V {first['y'] - 5}",
        }
    )
    edges.append(
        {
            "source": last["key"],
            "target": "end",
            "path": f"M {last['cx']} {last['bottom']} V {last['bottom'] + 38}",
        }
    )
    return {
        "width": len(stages) * lane_width + 20,
        "height": height,
        "lane_height": height - 20,
        "lanes": lanes,
        "nodes": nodes,
        "edges": edges,
        "returns": returns,
        "start_x": first["cx"],
        "end_x": last["cx"],
        "end_y": last["bottom"] + 66,
    }
