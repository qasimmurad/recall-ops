/* The Recall Ops kernel, in the browser.
 *
 * A port of src/kernel.py (and fleet_view from src/views.py) for the static
 * live demo, where there is no Python server. It interprets the same
 * ontology/ontology.json. tests/test_parity.py runs the same scenarios through
 * both kernels and fails if they disagree on a single edit or error message,
 * so the two can't drift apart silently.
 *
 * One deliberate difference: the Python kernel refuses to start on a damaged
 * edit log. Here the log is the visitor's own browser storage, so damaged
 * entries are skipped with a warning instead of breaking the demo.
 */
(function (root) {
  "use strict";

  class ActionError extends Error {
    constructor(message, field = null) {
      super(message);
      this.name = "ActionError";
      this.field = field;
    }
  }

  const ACTIVE = new Set(["open", "scheduled"]);
  const CLOSED = new Set(["completed", "dismissed"]);
  const EFFECT_KEYS = ["objectType", "primaryKey", "property"];

  const isObject = v => v !== null && typeof v === "object" && !Array.isArray(v);
  const isString = v => typeof v === "string";
  const has = (obj, key) => isObject(obj) && Object.prototype.hasOwnProperty.call(obj, key);
  const own = (obj, key) => (has(obj, key) ? obj[key] : undefined);
  const codePoints = s => [...s].length; // Python's len() counts code points, not UTF-16 units

  function nowIso() {
    return new Date().toISOString().replace(/\.\d{3}Z$/, "+00:00");
  }

  // Exactly YYYY-MM-DD and a real calendar date, as in the Python kernel.
  function parseDate(raw) {
    if (!/^[0-9]{4}-[0-9]{2}-[0-9]{2}$/.test(raw)) return null;
    const [y, m, d] = raw.split("-").map(Number);
    const probe = new Date(0);
    probe.setUTCFullYear(y, m - 1, d);
    const real = y >= 1 && probe.getUTCFullYear() === y && probe.getUTCMonth() === m - 1 && probe.getUTCDate() === d;
    return real ? raw : null;
  }

  function isEdit(edit) {
    return isObject(edit) && isString(edit.id) && Array.isArray(edit.effects)
      && edit.effects.every(e => isObject(e) && has(e, "after") && EFFECT_KEYS.every(k => isString(e[k])));
  }

  function applyEdit(objects, edit, orphaned) {
    for (const effect of edit.effects) {
      const target = own(own(objects, effect.objectType), effect.primaryKey);
      if (target === undefined) {
        orphaned.push({ edit: edit.id, ...effect });
        continue;
      }
      target[effect.property] = effect.after;
    }
  }

  function nextId(edits) {
    let max = 0;
    for (const edit of edits) {
      const match = /^E-([0-9]+)$/.exec(String(edit.id));
      if (match) max = Math.max(max, Number(match[1]));
    }
    return `E-${String(max + 1).padStart(4, "0")}`;
  }

  function validate(kernel, spec, params, today) {
    const declared = spec.parameters;
    const unknown = Object.keys(params).filter(k => !has(declared, k)).sort();
    if (unknown.length) throw new ActionError(`Unknown parameter: ${unknown.join(", ")}.`);

    const values = {};
    const refs = {};
    for (const [name, rule] of Object.entries(declared)) {
      const label = has(rule, "displayName") ? rule.displayName : name;
      let raw = own(params, name);
      if (isString(raw)) raw = raw.trim();
      if (raw === undefined || raw === null || raw === "") {
        if (rule.required) throw new ActionError(`${label} is required.`, name);
        values[name] = null;
        continue;
      }

      const kind = rule.type;
      if (kind === "objectRef") {
        if (!isString(raw)) throw new ActionError(`${label} must be an id.`, name);
        const target = kernel.get(rule.objectType, raw);
        if (target === undefined) throw new ActionError(`There is no ${rule.objectType} with id '${raw}'.`, name);
        refs[name] = [rule.objectType, raw, target];
        values[name] = raw;
      } else if (kind === "string" || kind === "text") {
        if (!isString(raw)) throw new ActionError(`${label} must be text.`, name);
        if (codePoints(raw) < (rule.minLength ?? 0)) {
          throw new ActionError(`${label} needs at least ${rule.minLength} characters.`, name);
        }
        if (codePoints(raw) > (rule.maxLength ?? 1000)) throw new ActionError(`${label} is too long.`, name);
        values[name] = raw;
      } else if (kind === "enum") {
        if (!isString(raw) || !rule.options.includes(raw)) {
          throw new ActionError(`${label} must be one of: ${rule.options.join(", ")}.`, name);
        }
        values[name] = raw;
      } else if (kind === "date") {
        const day = isString(raw) ? parseDate(raw) : null;
        if (day === null) throw new ActionError(`${label} must be a date (YYYY-MM-DD).`, name);
        if (rule.notBefore === "today" && day < today) throw new ActionError(`${label} can't be in the past.`, name);
        if (rule.notAfter === "today" && day > today) throw new ActionError(`${label} can't be in the future.`, name);
        values[name] = day;
      } else {
        throw new ActionError(`Unsupported parameter type '${kind}'.`);
      }
    }
    return { values, refs };
  }

  function check(kernel, criterion, refs) {
    const [objectType, primaryKey, target] = refs[criterion.object];
    if (criterion.type === "propertyIn") {
      if (!criterion.values.includes(target[criterion.property] ?? null)) throw new ActionError(criterion.message);
    } else if (criterion.type === "noLinked") {
      const blocking = kernel.linked(objectType, primaryKey, criterion.link)
        .filter(o => Object.entries(criterion.where).every(([prop, allowed]) => allowed.includes(o[prop] ?? null)));
      if (blocking.length) {
        const linkedType = kernel.ontology.linkTypes[criterion.link].to;
        const pkProp = kernel.ontology.objectTypes[linkedType].primaryKey;
        const ids = blocking.map(o => o[pkProp]).sort();
        const shown = ids.slice(0, 3).join(", ") + (ids.length > 3 ? ` and ${ids.length - 3} more` : "");
        throw new ActionError(`${criterion.message}: ${shown}.`);
      }
    } else {
      throw new ActionError(`Unsupported submission criterion '${criterion.type}'.`);
    }
  }

  function evaluate(expression, values, now) {
    if (isObject(expression)) {
      if (has(expression, "param")) return values[expression.param];
      if (expression.now) return now;
      throw new ActionError(`Unsupported value expression ${JSON.stringify(expression)}.`);
    }
    return expression;
  }

  function plan(ontology, spec, values, refs, now) {
    const effects = [];
    for (const edit of spec.edits) {
      if (edit.type !== "modify") throw new ActionError(`Unsupported edit type '${edit.type}'.`);
      const [objectType, primaryKey, target] = refs[edit.object];
      const properties = ontology.objectTypes[objectType].properties;
      for (const [prop, expression] of Object.entries(edit.set)) {
        if (!(own(properties, prop) || {}).editable) {
          throw new ActionError(`${objectType}.${prop} is not editable by actions.`);
        }
        const after = evaluate(expression, values, now);
        const before = target[prop] ?? null;
        if (before !== after) effects.push({ objectType, primaryKey, property: prop, before, after });
      }
    }
    return effects;
  }

  function createKernel(ontology, objects, savedEdits = []) {
    const edits = [];
    const orphaned = [];
    let skipped = 0;
    for (const edit of savedEdits) {
      if (!isEdit(edit)) {
        skipped += 1;
        continue;
      }
      applyEdit(objects, edit, orphaned);
      edits.push(edit);
    }

    const kernel = {
      ontology,
      objects,
      edits,
      orphaned,
      generatedAt: null,
      loadWarning: skipped ? `${skipped} saved action(s) couldn't be read and were skipped.` : null,

      get(objectType, primaryKey) {
        return own(own(objects, objectType), primaryKey);
      },

      linked(objectType, primaryKey, linkName) {
        const link = own(ontology.linkTypes, linkName);
        if (!link || link.from !== objectType) throw new Error(`Link ${linkName} doesn't start at ${objectType}`);
        return Object.values(own(objects, link.to) || {}).filter(o => o[link.foreignKey] === primaryKey);
      },

      snapshot(today) {
        const copy = {};
        for (const [kind, rows] of Object.entries(objects)) {
          copy[kind] = {};
          for (const [pk, obj] of Object.entries(rows)) copy[kind][pk] = { ...obj };
        }
        return {
          objects: copy,
          generatedAt: kernel.generatedAt,
          today,
          editCount: edits.length,
          orphanedEffects: orphaned.length,
          loadWarning: kernel.loadWarning,
        };
      },

      submit(actionName, params, actor, today, now = nowIso()) {
        if (params === null || params === undefined) params = {};
        if (!isObject(params)) throw new ActionError("params must be an object.");
        if (!isObject(actor) || !isString(actor.role)) throw new ActionError("actor must be an object with a role.");

        const spec = own(ontology.actionTypes, actionName);
        if (!spec) throw new ActionError(`Unknown action '${actionName}'.`);
        if (!spec.submitters.includes(actor.role)) {
          const allowed = spec.submitters.map(r => ontology.roles[r]).join(" or ");
          throw new ActionError(`Only a ${allowed.toLowerCase()} can ${spec.displayName.toLowerCase()}.`);
        }

        const { values, refs } = validate(kernel, spec, params, today);
        for (const criterion of spec.submissionCriteria || []) check(kernel, criterion, refs);
        const effects = plan(ontology, spec, values, refs, now);

        const name = actor.name;
        const edit = {
          id: nextId(edits),
          at: now,
          actor: { name: isString(name) && name.trim() ? name : "unknown", role: actor.role },
          action: actionName,
          params: values,
          effects,
        };
        applyEdit(objects, edit, orphaned);
        edits.push(edit);
        return edit;
      },
    };
    return kernel;
  }

  // Port of src/views.py: derived properties and headline numbers.
  function fleetView(snapshot) {
    const { today, objects } = snapshot;
    const vehicles = objects.Vehicle;
    const orders = objects.WorkOrder;

    for (const vehicle of Object.values(vehicles)) vehicle.openCritical = [];
    for (const order of Object.values(orders)) {
      order.overdue = ACTIVE.has(order.status) && order.dueBy < today;
      if (order.priority === "critical" && ACTIVE.has(order.status)) {
        vehicles[order.vehicleId].openCritical.push(order.workOrderId);
      }
    }
    for (const vehicle of Object.values(vehicles)) {
      vehicle.atRisk = vehicle.status === "in_service" && vehicle.openCritical.length > 0;
    }

    const count = (rows, test) => Object.values(rows).filter(test).length;
    return {
      objects,
      kpis: {
        atRisk: count(vehicles, v => v.atRisk),
        grounded: count(vehicles, v => v.status === "grounded"),
        active: count(orders, o => ACTIVE.has(o.status)),
        overdue: count(orders, o => o.overdue),
        closed: count(orders, o => CLOSED.has(o.status)),
        vehicles: Object.keys(vehicles).length,
        workOrders: Object.keys(orders).length,
      },
      meta: {
        generatedAt: snapshot.generatedAt,
        today,
        editCount: snapshot.editCount,
        orphanedEffects: snapshot.orphanedEffects,
        loadWarning: snapshot.loadWarning,
      },
    };
  }

  const api = { ActionError, createKernel, fleetView, nowIso };
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.RecallKernel = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
