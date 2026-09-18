"""Read the parameter tree the Universal Control app ships in quantumusbdefs.xml."""
from dataclasses import dataclass, field, asdict
import xml.etree.ElementTree as ET

DEFS_XML = ("/Applications/Universal Control.app/Contents/PlugIns/"
            "quantumpanel.bundle/Contents/Resources/quantumusbdefs.xml")


@dataclass
class Param:
    path: str
    id: str
    name: str
    type: str
    min: float | None
    max: float | None
    default: str | None
    units: str | None
    curve: str | None = None
    flags: list[str] = field(default_factory=list)
    component: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _num(v):
    return float(v) if v is not None else None


def _lists(root) -> dict[str, ET.Element]:
    return {pl.get("id"): pl for pl in root.findall(".//{*}ParamList")}


def _params_of(lists, list_id: str) -> list[ET.Element]:
    pl = lists[list_id]
    out = []
    if pl.get("inherit"):
        out += _params_of(lists, pl.get("inherit"))
    out += list(pl.findall("{*}Param"))
    if pl.get("mixin"):
        out += _params_of(lists, pl.get("mixin"))
    return out


def _walk(node, prefix, lists, out):
    for child in node:
        tag = child.tag.split("}")[-1]
        if tag == "foreach":
            var = child.get("id")
            for i in range(int(child.get("min")), int(child.get("max")) + 1):
                _walk_expanded(child, prefix, lists, out, var, str(i))
        elif tag == "Component":
            _component(child, child.get("id"), prefix, lists, out, None, None)


def _walk_expanded(node, prefix, lists, out, var, val):
    for child in node:
        if child.tag.split("}")[-1] == "Component":
            _component(child, child.get("id").replace(var, val), prefix, lists, out, var, val)


def _component(el, cid, prefix, lists, out, var, val):
    path = f"{prefix}/{cid}" if prefix else cid
    if el.get("paramlist"):
        for p in _params_of(lists, el.get("paramlist")):
            out.append(Param(
                path=f"{path}/{p.get('id')}", id=p.get("id"), name=p.get("name") or "",
                type=p.get("type"), min=_num(p.get("min")), max=_num(p.get("max")),
                default=p.get("def"), units=p.get("units"), curve=p.get("curve"),
                flags=(p.get("flags") or "").split(), component=path))
    _walk(el, path, lists, out)


def load_template(xml_path: str = DEFS_XML, template: str = "QuantumHd8") -> list[Param]:
    root = ET.parse(xml_path).getroot()
    lists = _lists(root)
    tpl = next(t for t in root.findall(".//{*}Template") if t.get("id") == template)
    out: list[Param] = []
    _walk(tpl, "", lists, out)
    return out
