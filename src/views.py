"""Read model for the Recall Ops app: derived properties and headline numbers.

Kept apart from the kernel on purpose. The kernel is generic; this is where
fleet-specific questions get answered, starting with the one the whole app
exists for: which vehicles are on the road right now with an unresolved
critical recall?
"""

ACTIVE = {"open", "scheduled"}
CLOSED = {"completed", "dismissed"}


def fleet_view(snapshot):
    today = snapshot["today"]
    objects = snapshot["objects"]
    vehicles = objects["Vehicle"]
    orders = objects["WorkOrder"]

    for vehicle in vehicles.values():
        vehicle["openCritical"] = []
    for order in orders.values():
        order["overdue"] = order["status"] in ACTIVE and order["dueBy"] < today
        if order["priority"] == "critical" and order["status"] in ACTIVE:
            vehicles[order["vehicleId"]]["openCritical"].append(order["workOrderId"])
    for vehicle in vehicles.values():
        vehicle["atRisk"] = vehicle["status"] == "in_service" and bool(vehicle["openCritical"])

    return {
        "objects": objects,
        "kpis": {
            "atRisk": sum(v["atRisk"] for v in vehicles.values()),
            "grounded": sum(v["status"] == "grounded" for v in vehicles.values()),
            "active": sum(o["status"] in ACTIVE for o in orders.values()),
            "overdue": sum(o["overdue"] for o in orders.values()),
            "closed": sum(o["status"] in CLOSED for o in orders.values()),
            "vehicles": len(vehicles),
            "workOrders": len(orders),
        },
        "meta": {
            "generatedAt": snapshot["generatedAt"],
            "today": today,
            "editCount": snapshot["editCount"],
            "orphanedEffects": snapshot["orphanedEffects"],
            "loadWarning": snapshot["loadWarning"],
        },
    }
