"""List catalog operations or generate a runnable one-task BPMN model."""

import argparse
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from catalog import Catalog, ConfigurationError
from worker import BPMN, EXTENSIONS

CAMUNDA = EXTENSIONS[0]
BPMNDI = "{http://www.omg.org/spec/BPMN/20100524/DI}"
DC = "{http://www.omg.org/spec/DD/20100524/DC}"
DI = "{http://www.omg.org/spec/DD/20100524/DI}"
ET.register_namespace("bpmn", BPMN[1:-1])
ET.register_namespace("camunda", CAMUNDA[1:-1])
ET.register_namespace("bpmndi", BPMNDI[1:-1])
ET.register_namespace("dc", DC[1:-1])
ET.register_namespace("di", DI[1:-1])


def build_model(catalog, operation_id, process_key, topic):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", process_key):
        raise ConfigurationError("Use a valid BPMN process key")
    endpoint = catalog.endpoint(operation_id)
    item, operation = catalog.operations[endpoint["method"], endpoint["path"]]
    inputs = []
    used_names = {"icore_result", "requestBody"}
    for (location, name), parameter in catalog.parameters(item, operation).items():
        if not parameter.get("required") and location != "path":
            continue
        variable = re.sub(r"[^A-Za-z0-9_]", "_", name)
        if not re.match(r"[A-Za-z_]", variable):
            variable = "arg_" + variable
        while variable in used_names:
            variable = location + "_" + variable
        used_names.add(variable)
        inputs.append({"target": location + "." + name, "source": {"kind": "variable", "path": variable}})
    if catalog.request_body(operation).get("required"):
        inputs.append({"target": "body", "source": {"kind": "variable", "path": "requestBody"}})
    config = {
        "version": 1, "endpoint": endpoint, "connection": "default",
        "inputs": inputs, "resultVariable": "icore_result",
    }
    root = ET.Element(BPMN + "definitions", {"id": "Definitions_1", "targetNamespace": "urn:icore:worker"})
    process = ET.SubElement(root, BPMN + "process", {"id": process_key, "isExecutable": "true"})
    ET.SubElement(process, BPMN + "startEvent", {"id": "Start"})
    task = ET.SubElement(process, BPMN + "serviceTask", {
        "id": "ICoreTask", "name": operation_id, CAMUNDA + "type": "external", CAMUNDA + "topic": topic,
    })
    extensions = ET.SubElement(task, BPMN + "extensionElements")
    properties = ET.SubElement(extensions, CAMUNDA + "properties")
    ET.SubElement(properties, CAMUNDA + "property", {"name": "icore-config", "value": json.dumps(config)})
    ET.SubElement(process, BPMN + "endEvent", {"id": "End"})
    for index, (source, target) in enumerate([("Start", "ICoreTask"), ("ICoreTask", "End")], start=1):
        ET.SubElement(process, BPMN + "sequenceFlow", {"id": f"Flow_{index}", "sourceRef": source, "targetRef": target})
    diagram = ET.SubElement(root, BPMNDI + "BPMNDiagram", {"id": "Diagram_1"})
    plane = ET.SubElement(diagram, BPMNDI + "BPMNPlane", {"id": "Plane_1", "bpmnElement": process_key})
    for node, x, y, width, height in [("Start", 120, 122, 36, 36), ("ICoreTask", 215, 100, 100, 80),
                                      ("End", 375, 122, 36, 36)]:
        shape = ET.SubElement(plane, BPMNDI + "BPMNShape", {"id": node + "_di", "bpmnElement": node})
        ET.SubElement(shape, DC + "Bounds", {"x": str(x), "y": str(y), "width": str(width), "height": str(height)})
    for flow, start, end in [("Flow_1", 156, 215), ("Flow_2", 315, 375)]:
        edge = ET.SubElement(plane, BPMNDI + "BPMNEdge", {"id": flow + "_di", "bpmnElement": flow})
        for x in (start, end):
            ET.SubElement(edge, DI + "waypoint", {"x": str(x), "y": "140"})
    ET.indent(root)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True), inputs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="List operations and explain exclusions")
    model = commands.add_parser("bpmn", help="Write a one-task Operaton BPMN file")
    model.add_argument("operation_id")
    model.add_argument("--output", required=True)
    model.add_argument("--process-key", default="icore_dto_example")
    model.add_argument("--topic", default="icore-service")
    args = parser.parse_args()
    try:
        catalog = Catalog(args.catalog)
        if args.command == "list":
            operations = []
            for (method, path), (item, operation) in catalog.operations.items():
                operations.append({
                    "operationId": operation.get("operationId"), "method": method, "path": path,
                    "excluded": catalog.exclusion(path, item, operation),
                })
            print(json.dumps({"catalogHash": catalog.hash, "operations": operations}, indent=2))
        else:
            xml, inputs = build_model(catalog, args.operation_id, args.process_key, args.topic)
            with Path(args.output).open("xb") as output:
                output.write(xml)
            print(json.dumps({"processKey": args.process_key, "inputs": inputs}, indent=2))
    except (ConfigurationError, OSError, KeyError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
