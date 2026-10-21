"""Layout the sequential definition as four SVG stage lanes."""

from textwrap import wrap


def build_diagram(stages, *, node_height=94, node_gap=42):
    lane_width, node_width = 360, 260
    nodes, lanes, edges, returns = [], [], [], []
    height = 220 + max(
        sum(node.get("height", node_height) + node_gap for node in stage["nodes"])
        for stage in stages
    )
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
        y = 144
        for node in stage["nodes"]:
            x = lane_x + 80
            actual_height = node.get("height", node_height)
            nodes.append(
                {
                    **node,
                    "x": x,
                    "y": y,
                    "cx": x + node_width / 2,
                    "height": actual_height,
                    "bottom": y + actual_height,
                    "middle": y + actual_height / 2,
                    "label_lines": [
                        {"text": line, "y": y + 29 + index * 20}
                        for index, line in enumerate(wrap(node["name"], 17))
                    ],
                    "role_y": y + 78,
                    "number_x": x + 12,
                    "number_y": y + 16,
                }
            )
            y += actual_height + node_gap
    by_key = {node["key"]: node for node in nodes}
    main_nodes = [node for node in nodes if not node.get("return_only")]
    for index, node in enumerate(main_nodes):
        if index + 1 < len(main_nodes):
            target = main_nodes[index + 1]
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
    for correction in (node for node in nodes if node.get("return_only")):
        target = by_key["order_review"]
        corridor = correction["x"] + 268
        edges.append(
            {
                "source": correction["key"],
                "target": target["key"],
                "path": (
                    f"M {correction['cx']} {correction['bottom']} V {correction['bottom'] + 14} "
                    f"H {corridor} V {target['middle']} H {target['x'] + node_width + 5}"
                ),
            }
        )
    first, last = main_nodes[0], main_nodes[-1]
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
