"""The ontology kernel: typed objects, links, and actions as the only write path.

A small, generic interpreter for ontology/ontology.json. It knows nothing
about vehicles or recalls. Everything domain-specific (object types, links,
who may submit each action, what it validates, what it edits) is declared in
the JSON, so adding an action that uses the supported parameter, criterion
and edit types is a data change, not a code change.

State has two layers:
  state/objects.json   rebuilt by the pipeline from source data
  state/edits.jsonl    append-only log of every action a person submitted

Edits are replayed over the pipeline output on load, so re-running the
pipeline never erases a human decision. Each edit records who acted, the
parameters, and the before and after value of every property it touched.
The kernel assumes it is the only process writing the log.

Reading the log fails loudly, with one exception. A torn final line (the
process died mid-append) is moved to edits.jsonl.torn and the rest loads.
Corruption anywhere else stops the kernel and names the line: deciding which
human decisions to keep is not the kernel's call.
"""

import json
import os
import pathlib
import re
import threading
from datetime import date, datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]


class ActionError(Exception):
    """A submission was rejected. `field` names the offending parameter, if any."""

    def __init__(self, message, field=None):
        super().__init__(message)
        self.message = message
        self.field = field


class LogCorrupted(Exception):
    """The edit log has a damaged line that is not a torn final append."""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _signature(path):
    """Cheap change detection: (inode, size, mtime), or None if the file is missing."""
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return (stat.st_ino, stat.st_size, stat.st_mtime_ns)


def _write_atomic(path, text):
    temp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temp.write_text(text)
    os.replace(temp, path)


class Kernel:
    def __init__(self,
                 ontology_path=ROOT / "ontology" / "ontology.json",
                 objects_path=ROOT / "state" / "objects.json",
                 edits_path=ROOT / "state" / "edits.jsonl",
                 today=None):
        self.ontology_path = pathlib.Path(ontology_path)
        self.objects_path = pathlib.Path(objects_path)
        self.edits_path = pathlib.Path(edits_path)
        self._today = today
        self._lock = threading.RLock()
        self.load_warning = None
        self.reload()

    def today(self):
        return self._today or date.today()

    # -- loading ------------------------------------------------------------

    def reload(self):
        with self._lock:
            # Signatures are taken before reading, so a write that lands mid-read
            # shows up as a change on the next check instead of being missed.
            objects_signature = _signature(self.objects_path)
            edits_signature = _signature(self.edits_path)

            ontology = json.loads(self.ontology_path.read_text())
            base = json.loads(self.objects_path.read_text())
            objects = base["objects"]
            edits, orphaned, warning = self._read_log(objects)
            if warning:
                edits_signature = _signature(self.edits_path)

            # Swap in the new state only once everything above has succeeded.
            # This also clears any warning left over from an earlier failed reload.
            self.ontology = ontology
            self.objects = objects
            self.generated_at = base.get("generatedAt")
            self.edits = edits
            self.orphaned = orphaned
            self.load_warning = warning
            self._signatures = (objects_signature, edits_signature)

    @staticmethod
    def _parse_edit(line):
        """Parse one log line, or raise ValueError if it isn't a well-formed edit."""
        edit = json.loads(line)
        if not isinstance(edit, dict) or not isinstance(edit.get("id"), str) or not isinstance(edit.get("effects"), list):
            raise ValueError("not an edit")
        for effect in edit["effects"]:
            if not (isinstance(effect, dict) and "after" in effect
                    and all(isinstance(effect.get(k), str) for k in ("objectType", "primaryKey", "property"))):
                raise ValueError("malformed effect")
        return edit

    def _read_log(self, objects):
        """Returns (edits, orphaned effects, warning or None). Applies edits to `objects`."""
        edits, orphaned = [], []
        if not self.edits_path.exists():
            return edits, orphaned, None

        content = self.edits_path.read_text()
        lines = content.split("\n")
        # Every append ends with a newline, so a complete log splits into a
        # trailing empty string. Anything else there is an append that died.
        tail = lines.pop()

        for number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                edit = self._parse_edit(line)
            except ValueError:
                raise LogCorrupted(f"{self.edits_path} line {number} is damaged. "
                                   "Fix or remove it by hand; the kernel won't guess.") from None
            self._apply(objects, edit, orphaned)
            edits.append(edit)

        if not tail.strip():
            return edits, orphaned, None

        try:
            edit = self._parse_edit(tail)
        except ValueError:
            with self.edits_path.with_name(self.edits_path.name + ".torn").open("a") as handle:
                handle.write(tail + "\n")
            warning = "A torn final line in the edit log was moved to edits.jsonl.torn."
        else:
            # Complete JSON that only lost its newline: keep it.
            self._apply(objects, edit, orphaned)
            edits.append(edit)
            warning = "The edit log's last line was missing its newline; it was repaired."
        _write_atomic(self.edits_path, "".join(json.dumps(e) + "\n" for e in edits))
        return edits, orphaned, warning

    def refresh_if_rebuilt(self):
        """Pick up a pipeline rebuild, or an edited or deleted log, without a restart."""
        current = (_signature(self.objects_path), _signature(self.edits_path))
        if current == self._signatures:
            return
        try:
            self.reload()
        except (OSError, ValueError, KeyError, TypeError, LogCorrupted) as error:
            # Keep serving the last consistent state rather than half of a new one.
            # The next successful reload clears this warning.
            self.load_warning = f"Reload failed, still serving the previous state: {error}"

    @staticmethod
    def _apply(objects, edit, orphaned):
        for effect in edit["effects"]:
            target = objects.get(effect["objectType"], {}).get(effect["primaryKey"])
            if target is None:
                # The pipeline no longer produces this object. Keep the edit in
                # the log, but surface it rather than silently dropping it.
                orphaned.append({"edit": edit["id"], **effect})
                continue
            target[effect["property"]] = effect["after"]

    # -- reading ------------------------------------------------------------

    def get(self, object_type, primary_key):
        return self.objects.get(object_type, {}).get(primary_key)

    def linked(self, object_type, primary_key, link_name):
        link = self.ontology["linkTypes"][link_name]
        if link["from"] != object_type:
            raise KeyError(f"Link {link_name} starts at {link['from']}, not {object_type}")
        return [o for o in self.objects.get(link["to"], {}).values() if o.get(link["foreignKey"]) == primary_key]

    def snapshot(self):
        """A consistent copy of current state for read models to work from."""
        with self._lock:
            self.refresh_if_rebuilt()
            return {
                "objects": {kind: {pk: dict(obj) for pk, obj in rows.items()} for kind, rows in self.objects.items()},
                "generatedAt": self.generated_at,
                "today": self.today().isoformat(),
                "editCount": len(self.edits),
                "orphanedEffects": len(self.orphaned),
                "loadWarning": self.load_warning,
            }

    def edit_log(self):
        with self._lock:
            self.refresh_if_rebuilt()
            return list(self.edits)

    # -- writing ------------------------------------------------------------

    def submit(self, action_name, params, actor):
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise ActionError("params must be an object.")
        if not isinstance(actor, dict) or not isinstance(actor.get("role"), str):
            raise ActionError("actor must be an object with a role.")

        with self._lock:
            self.refresh_if_rebuilt()
            spec = self.ontology["actionTypes"].get(action_name)
            if spec is None:
                raise ActionError(f"Unknown action '{action_name}'.")

            if actor["role"] not in spec["submitters"]:
                allowed = " or ".join(self.ontology["roles"][r] for r in spec["submitters"])
                raise ActionError(f"Only a {allowed.lower()} can {spec['displayName'].lower()}.")

            values, refs = self._validate(spec, params)
            for criterion in spec.get("submissionCriteria", []):
                self._check(criterion, refs)
            effects = self._plan(spec, values, refs)

            name = actor.get("name")
            edit = {
                "id": self._next_id(),
                "at": _now(),
                "actor": {"name": name if isinstance(name, str) and name.strip() else "unknown", "role": actor["role"]},
                "action": action_name,
                "params": values,
                "effects": effects,
            }
            self._append(edit)
            self._apply(self.objects, edit, self.orphaned)
            self.edits.append(edit)
            return edit

    def _next_id(self):
        numbers = [int(m.group(1)) for e in self.edits if (m := re.fullmatch(r"E-(\d+)", str(e.get("id"))))]
        return f"E-{max(numbers, default=0) + 1:04d}"

    def _append(self, edit):
        self.edits_path.parent.mkdir(parents=True, exist_ok=True)
        with self.edits_path.open("a") as handle:
            handle.write(json.dumps(edit) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        # Our own append is not an outside change, so don't trigger a reload for it.
        self._signatures = (self._signatures[0], _signature(self.edits_path))

    def _validate(self, spec, params):
        declared = spec["parameters"]
        unknown = sorted(set(params) - set(declared))
        if unknown:
            raise ActionError(f"Unknown parameter: {', '.join(unknown)}.")

        values, refs = {}, {}
        for name, rule in declared.items():
            label = rule.get("displayName", name)
            raw = params.get(name)
            if isinstance(raw, str):
                raw = raw.strip()
            if raw is None or raw == "":
                if rule.get("required"):
                    raise ActionError(f"{label} is required.", name)
                values[name] = None
                continue

            kind = rule["type"]
            if kind == "objectRef":
                if not isinstance(raw, str):
                    raise ActionError(f"{label} must be an id.", name)
                target = self.get(rule["objectType"], raw)
                if target is None:
                    raise ActionError(f"There is no {rule['objectType']} with id '{raw}'.", name)
                refs[name] = (rule["objectType"], raw, target)
                values[name] = raw
            elif kind in ("string", "text"):
                if not isinstance(raw, str):
                    raise ActionError(f"{label} must be text.", name)
                if len(raw) < rule.get("minLength", 0):
                    raise ActionError(f"{label} needs at least {rule['minLength']} characters.", name)
                if len(raw) > rule.get("maxLength", 1000):
                    raise ActionError(f"{label} is too long.", name)
                values[name] = raw
            elif kind == "enum":
                if not isinstance(raw, str) or raw not in rule["options"]:
                    raise ActionError(f"{label} must be one of: {', '.join(rule['options'])}.", name)
                values[name] = raw
            elif kind == "date":
                # Exactly YYYY-MM-DD. Newer Pythons' fromisoformat also accepts forms
                # like 20260930, and the browser kernel must agree with this one.
                if not isinstance(raw, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", raw):
                    raise ActionError(f"{label} must be a date (YYYY-MM-DD).", name)
                try:
                    day = date.fromisoformat(raw)
                except ValueError:
                    raise ActionError(f"{label} must be a date (YYYY-MM-DD).", name) from None
                if rule.get("notBefore") == "today" and day < self.today():
                    raise ActionError(f"{label} can't be in the past.", name)
                if rule.get("notAfter") == "today" and day > self.today():
                    raise ActionError(f"{label} can't be in the future.", name)
                values[name] = day.isoformat()
            else:
                raise ActionError(f"Unsupported parameter type '{kind}'.")
        return values, refs

    def _check(self, criterion, refs):
        object_type, primary_key, target = refs[criterion["object"]]
        kind = criterion["type"]
        if kind == "propertyIn":
            if target.get(criterion["property"]) not in criterion["values"]:
                raise ActionError(criterion["message"])
        elif kind == "noLinked":
            blocking = [
                o for o in self.linked(object_type, primary_key, criterion["link"])
                if all(o.get(prop) in allowed for prop, allowed in criterion["where"].items())
            ]
            if blocking:
                linked_type = self.ontology["linkTypes"][criterion["link"]]["to"]
                pk_prop = self.ontology["objectTypes"][linked_type]["primaryKey"]
                ids = sorted(o[pk_prop] for o in blocking)
                shown = ", ".join(ids[:3]) + (f" and {len(ids) - 3} more" if len(ids) > 3 else "")
                raise ActionError(f"{criterion['message']}: {shown}.")
        else:
            raise ActionError(f"Unsupported submission criterion '{kind}'.")

    def _plan(self, spec, values, refs):
        effects = []
        now = _now()
        for edit in spec["edits"]:
            if edit["type"] != "modify":
                raise ActionError(f"Unsupported edit type '{edit['type']}'.")
            object_type, primary_key, target = refs[edit["object"]]
            properties = self.ontology["objectTypes"][object_type]["properties"]
            for prop, expression in edit["set"].items():
                if not properties.get(prop, {}).get("editable"):
                    raise ActionError(f"{object_type}.{prop} is not editable by actions.")
                after = self._evaluate(expression, values, now)
                before = target.get(prop)
                if before != after:
                    effects.append({
                        "objectType": object_type,
                        "primaryKey": primary_key,
                        "property": prop,
                        "before": before,
                        "after": after,
                    })
        return effects

    @staticmethod
    def _evaluate(expression, values, now):
        if isinstance(expression, dict):
            if "param" in expression:
                return values[expression["param"]]
            if expression.get("now"):
                return now
            raise ActionError(f"Unsupported value expression {expression}.")
        return expression
