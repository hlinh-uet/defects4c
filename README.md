# Defects4C for Debugging-Framework

Fork of [Defects4C](https://github.com/defects4c/defects4c) with scripts that
prepare its bugs as inputs for
[Debugging-Framework](https://github.com/hlinh-uet/Debugging-Framework).

## Instructions for the PHP project

### Requirements

- Docker (running), Python 3.10+ and Git.
- Debugging-Framework installed (see its README) in a folder next to this one:

```text
some-folder/
├── Debugging-Framework/
└── defects4c/
```

### Run

From the `defects4c/` folder:

```bash
# 1. Build the Docker image (once)
docker build -f Dockerfile.php -t php-src/defect4c:latest .

# 2. List the 12 bugs
python3 defectsc_tpl/projects/php___php-src/run_debugging_case.py --list

# 3. Run one bug (prepare inputs -> doctor -> repair)
python3 defectsc_tpl/projects/php___php-src/run_debugging_case.py \
  trial --sha CVE-2016-5096

# Or run all 12 bugs
python3 defectsc_tpl/projects/php___php-src/run_debugging_case.py \
  trial --all --continue-on-error
```

### Outputs

- Inputs: `out_tmp_dirs/debugging_framework/php/inputs/` (reused on later runs;
  add `--force` to rebuild).
- Results: Debugging-Framework's results directory
  (`Debugging-Framework/experiments/` by default).
