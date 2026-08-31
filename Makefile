.PHONY: data baseline solve eval report test all

data:       ## M2: generate deterministic datasets + ground truth
	PYTHONPATH=src python -m recon.generate --seed 42 --out data

baseline:   ## M4: run one-prompt baseline on all cases
	PYTHONPATH=src python -m recon.baseline --data data --outputs outputs/baseline

solve:      ## M5: run agent solution on all cases
	PYTHONPATH=src python -m recon.agent --data data --outputs outputs/solution

eval:       ## M3: score outputs against ground truth
	PYTHONPATH=src python -m recon.evaluate --outputs outputs/baseline --data data --out results/baseline.json && PYTHONPATH=src python -m recon.evaluate --outputs outputs/solution --data data --out results/solution.json && PYTHONPATH=src python -m recon.evaluate --compare results/baseline.json results/solution.json --out results/comparison.json

report:     ## M6: XLSX exception reports from solution outputs
	PYTHONPATH=src python -m recon.report --outputs outputs/solution --data data --out reports

test:
	PYTHONPATH=src python -m pytest -q

all: data baseline solve eval
