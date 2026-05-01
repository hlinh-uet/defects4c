# `fmtlib___fmt` - Defects4C x Unified-Debugging

## Structure

```text
defects4c/
├── Dockerfile.fmt
├── defectsc_tpl/projects_v1/fmtlib___fmt/
│   ├── bugs_list_new.json
│   ├── project.json
│   ├── build_meta_fmt.py
│   └── README.md
└── out_tmp_dirs/
    ├── fmtlib___fmt/git_repo_dir_<sha>/
    └── unified_debugging/fmt/
        ├── metadata/
        └── raw/
```

## Pipeline

`build_meta_fmt.py` uses the same buggy/fixed convention as the other
Defects4C projects:

1. Fixed version: checkout `commit_after`.
2. Buggy version: checkout `commit_after`, then overlay `files.src` from
   `commit_before`.
3. Phase A: build without sanitizer by default and run CTest on buggy to
   collect `outcome`.
4. Phase A fixed: build fixed without sanitizer by default and run the same
   test list to collect `outcome_fixed`.
5. Phase B: build buggy without sanitizer but with gcov flags, run tests one by one,
   clear `.gcda` before each test, and collect `covered_functions`.
6. Write full gcov coverage to `raw/`; write production-only coverage to
   `metadata/`.

`metadata` keeps only functions from `include/fmt/*` and `src/*`. Test and
harness code under `test/*` is preserved in `raw`, but removed from
`metadata` so Unified-Debugging FL ranks production code by default.

`--jobs 4` only controls Ninja build parallelism. Tests and coverage collection
still run sequentially to avoid `.gcda` files from different tests overwriting
each other.

Use `--phase-a-asan` only when you explicitly want ASAN/UBSAN labels. The
default `--phase-a-plain` is recommended for fmt because several historical
fmt tests intentionally exercise exceptional allocation/assertion paths that
ASAN can abort before the test framework handles them.

## Docker

Build the image:

```bash
cd "/Users/linhnh/Documents/Fault Localization/defects4c"
docker build -f Dockerfile.fmt -t fmt/defect4c:latest .
```

Create a long-running container:

```bash
docker rm -f my_defects4c_fmt 2>/dev/null || true
docker run -d --name my_defects4c_fmt \
  --ipc=host \
  -v "$(pwd)/defectsc_tpl:/src" \
  -v "$(pwd)/out_tmp_dirs:/out" \
  -v "$(pwd)/patche_dirs:/patches" \
  -v "$(pwd)/unified_debugging:/unified_debugging" \
  -v "$(pwd)/../Unified-Debugging:/udbg" \
  fmt/defect4c:latest sleep infinity
```

Clone repos once:

```bash
docker exec my_defects4c_fmt bash -lc \
  'cd /src && bash bulk_git_clone_v2.sh mini fmtlib___fmt'
```

If the clone helper is not available or does not include the project, the
metadata script can clone missing repos with `--clone`.

## Run

Run one bug:

```bash
docker exec my_defects4c_fmt bash -lc '
  cd /src/projects_v1/fmtlib___fmt && \
  python3 build_meta_fmt.py \
    --sha 6a1346405949ca1cf5befc5e83c6c66c86e4f9d1 \
    --metadata-dir /out/unified_debugging/fmt/metadata \
    --raw-dir /out/unified_debugging/fmt/raw \
    --jobs 4 \
    --clone
'
```

Run one bug with ASAN/UBSAN labels, only for sanitizer-specific debugging:

```bash
docker exec my_defects4c_fmt bash -lc '
  cd /src/projects_v1/fmtlib___fmt && \
  python3 build_meta_fmt.py \
    --sha 6a1346405949ca1cf5befc5e83c6c66c86e4f9d1 \
    --metadata-dir /out/unified_debugging/fmt/metadata \
    --raw-dir /out/unified_debugging/fmt/raw \
    --jobs 4 \
    --phase-a-asan \
    --clone
'
```

Run all bugs:

```bash
docker exec my_defects4c_fmt bash -lc '
  cd /src/projects_v1/fmtlib___fmt && \
  python3 build_meta_fmt.py \
    --metadata-dir /out/unified_debugging/fmt/metadata \
    --raw-dir /out/unified_debugging/fmt/raw \
    --jobs 4 \
    --skip-if-exists \
    --clone
'
```

Debug faster with only test names from `c_compile.test_flags`:

```bash
docker exec my_defects4c_fmt bash -lc '
  cd /src/projects_v1/fmtlib___fmt && \
  python3 build_meta_fmt.py \
    --sha 6a1346405949ca1cf5befc5e83c6c66c86e4f9d1 \
    --metadata-dir /out/unified_debugging/fmt/metadata \
    --raw-dir /out/unified_debugging/fmt/raw \
    --jobs 4 \
    --trigger-tests-only \
    --clone
'
```

## Process Lock

The script uses a lock under `/tmp`, so only one metadata process can use the
same fmt output tree at a time. Check and stop a running process inside the fmt
container:

```bash
docker exec my_defects4c_fmt bash -lc \
  'ps -eo pid,ppid,stat,cmd | grep "[p]ython3 build_meta_fmt.py" || true'
```

```bash
docker exec my_defects4c_fmt bash -lc 'kill <pid>'
```

## Output Check

```bash
docker exec my_defects4c_fmt bash -lc 'python3 - <<'"'"'PY'"'"'
import json
p="/out/unified_debugging/fmt/metadata/B__6a1346405949_meta.json"
d=json.load(open(p))
print("tests", len(d.get("tests", [])))
print("phase", d.get("phase_info"))
print("buggy_fail", [t["test_id"] for t in d.get("tests", []) if t["outcome"] == "FAIL"])
print("fixed_fail", [t["test_id"] for t in d.get("tests", []) if t["outcome_fixed"] == "FAIL"])
print("with_cov", sum(1 for t in d.get("tests", []) if t.get("covered_functions")))
PY'
```
