# Datasets

| folder | items | what |
|---|---|---|
| `automationbench/` | 244 | AutomationBench (Zapier, MIT) public tasks on myworld, graded by its own assertions |
| `automationbench-runtime/` | 1345 | perturbed (mutate) and resume (snapshot, fork) splits of those tasks |
| `safety/` | 20 | counterfactual safety pairs on ClawsBench's themes (original scenarios) |

Run a model: `myworld eval <folder> --model claude-opus-5 -o results/<name>`. Load specs in code:
`myworld.bench.runner.load_dataset(folder)`.
