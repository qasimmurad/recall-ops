// Runs scenario steps through web/kernel.js and prints the results as JSON.
// tests/test_parity.py sends the same steps through src/kernel.py and compares.
const fs = require("fs");
const path = require("path");
const { createKernel, fleetView } = require(path.join(__dirname, "..", "web", "kernel.js"));

const input = JSON.parse(fs.readFileSync(0, "utf8"));
const kernel = createKernel(input.ontology, input.objects, []);
kernel.generatedAt = input.generatedAt;

const results = input.steps.map(([action, params, actor]) => {
  try {
    return { edit: kernel.submit(action, params, actor, input.today, input.now) };
  } catch (error) {
    if (error.name !== "ActionError") throw error;
    return { error: error.message, field: error.field };
  }
});

process.stdout.write(JSON.stringify({ results, view: fleetView(kernel.snapshot(input.today)) }));
