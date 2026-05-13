# `danmar___cppcheck` - Defects4C x Unified-Debugging

## Structure

```text
defects4c/
├── Dockerfile.cppcheck
├── defectsc_tpl/projects_v1/danmar___cppcheck/
│   ├── bugs_list_new.json
│   ├── project.json
│   ├── build_meta_cppcheck.py
│   └── README.md
└── out_tmp_dirs/
    ├── danmar___cppcheck/git_repo_dir_<sha>/
    └── unified_debugging/cppcheck/
        ├── metadata/
        └── raw/
```

## Pipeline

`build_meta_cppcheck.py` follows the same Defects4C convention as the other
project scripts:

1. Fixed version: checkout `commit_after`.
2. Buggy version: checkout `commit_after`, then overlay `files.src` from
   `commit_before`.
3. Phase A: build without sanitizer by default and run cppcheck `testrunner`
   subtests on buggy to collect `outcome`.
4. Phase A fixed: build fixed without sanitizer by default and run the same
   test list to collect `outcome_fixed`.
5. Phase B: build buggy without sanitizer but with gcov flags, run tests one by one,
   clear `.gcda` before each test, and collect `covered_functions`.
6. Write full gcov coverage to `raw/`; write production-only coverage to
   `metadata/`.

`metadata` keeps production files such as `lib/*`, `cli/*`, and selected
production C/C++ files. Test/harness coverage under `test/*` is preserved in
`raw` but removed from `metadata`.

Cppcheck's CTest suite exposes only one wrapper test named `testrunner`, while
Defects4C points to cppcheck's own `TestFixture` names such as `TestCondition`
and `TestValueFlow`. The script therefore expands tests by default to individual
`TestClass::testCase` entries, orders the Defects4C trigger tests first, then
applies `--max-tests`. Revision `099b4435c38d` has 3070 subtests, so the default
`--max-tests 50` avoids running the whole suite unless explicitly requested.
Use `--max-tests 0` only when you intentionally want all discovered tests.

`--jobs 4` only controls Ninja build parallelism. Tests and coverage collection
run sequentially to avoid `.gcda` from different tests mixing together.

Use `--phase-a-asan` only for sanitizer-specific debugging. The default
`--phase-a-plain` is recommended for cppcheck because older revisions can fail
to compile or run under modern GCC ASAN/UBSAN even when the fixed plain build
is valid.

Some older cppcheck revisions also fail to compile on modern GCC/glibc because
`SIGSTKSZ` is no longer accepted as a static array bound under the old
`-std=c++0x` flags used by the project, or because headers such as `<limits>`
were previously included transitively. The script applies temporary build-only
compatibility patches after each checkout. These patches are applied
consistently to buggy, fixed, and gcov builds and are recorded in
`phase_info.build_compat_patches`.

## Docker

Build the image:

```bash
cd "/Users/linhnh/Documents/Fault Localization/defects4c"
docker build -f Dockerfile.cppcheck -t cppcheck/defect4c:latest .
```

Create a long-running container:

```bash
docker rm -f my_defects4c_cppcheck 2>/dev/null || true
docker run -d --name my_defects4c_cppcheck \
  --ipc=host \
  -v "$(pwd)/defectsc_tpl:/src" \
  -v "$(pwd)/out_tmp_dirs:/out" \
  -v "$(pwd)/patche_dirs:/patches" \
  -v "$(pwd)/unified_debugging:/unified_debugging" \
  -v "$(pwd)/../Unified-Debugging:/udbg" \
  cppcheck/defect4c:latest sleep infinity
```

Clone repos once:

```bash
docker exec my_defects4c_cppcheck bash -lc \
  'cd /src && bash bulk_git_clone_v2.sh mini danmar___cppcheck'
```

If the clone helper is unavailable for this project, use `--clone` in
`build_meta_cppcheck.py`.

## Run

Run one bug:

```bash
docker exec my_defects4c_cppcheck bash -lc '
  cd /src/projects_v1/danmar___cppcheck && \
  python3 build_meta_cppcheck.py \
    --sha 099b4435c38dd52ddb38e6b1706d9c988699c082 \
    --metadata-dir /out/unified_debugging/cppcheck/metadata \
    --raw-dir /out/unified_debugging/cppcheck/raw \
    --jobs 4 \
    --max-tests 50 \
    --clone
'
```

Run one bug with ASAN/UBSAN labels, only for sanitizer-specific debugging:

```bash
docker exec my_defects4c_cppcheck bash -lc '
  cd /src/projects_v1/danmar___cppcheck && \
  python3 build_meta_cppcheck.py \
    --sha 099b4435c38dd52ddb38e6b1706d9c988699c082 \
    --metadata-dir /out/unified_debugging/cppcheck/metadata \
    --raw-dir /out/unified_debugging/cppcheck/raw \
    --jobs 4 \
    --max-tests 50 \
    --phase-a-asan \
    --clone
'
```

Run all bugs:

```bash
docker exec my_defects4c_cppcheck bash -lc '
  cd /src/projects_v1/danmar___cppcheck && \
  python3 build_meta_cppcheck.py \
    --metadata-dir /out/unified_debugging/cppcheck/metadata \
    --raw-dir /out/unified_debugging/cppcheck/raw \
    --jobs 4 \
    --max-tests 70 \
    --skip-if-exists \
    --clone
'
```

`--skip-if-exists` skips only metadata already produced with the requested
test granularity and max-test setting. Older wrapper-only metadata is detected
and regenerated.

Run one bug with all individual subtests. This is much slower because Phase B
runs gcov once per subtest:

```bash
docker exec my_defects4c_cppcheck bash -lc '
  cd /src/projects_v1/danmar___cppcheck && \
  python3 build_meta_cppcheck.py \
    --sha 099b4435c38dd52ddb38e6b1706d9c988699c082 \
    --metadata-dir /out/unified_debugging/cppcheck/metadata \
    --raw-dir /out/unified_debugging/cppcheck/raw \
    --jobs 4 \
    --test-granularity subtest \
    --max-tests 0 \
    --clone
'
```

Debug faster with only test names from `c_compile.test_flags`:

```bash
docker exec my_defects4c_cppcheck bash -lc '
  cd /src/projects_v1/danmar___cppcheck && \
  python3 build_meta_cppcheck.py \
    --sha 099b4435c38dd52ddb38e6b1706d9c988699c082 \
    --metadata-dir /out/unified_debugging/cppcheck/metadata \
    --raw-dir /out/unified_debugging/cppcheck/raw \
    --jobs 4 \
    --trigger-tests-only \
    --clone
'
```

## Process Lock

The script uses a lock under `/tmp`, so only one metadata process can use the
same cppcheck output tree at a time.

```bash
docker exec my_defects4c_cppcheck bash -lc \
  'ps -eo pid,ppid,stat,cmd | grep "[p]ython3 build_meta_cppcheck.py" || true'
```

```bash
docker exec my_defects4c_cppcheck bash -lc 'kill <pid>'
```

## Output Check

```bash
docker exec my_defects4c_cppcheck bash -lc 'python3 - <<'"'"'PY'"'"'
import json
p="/out/unified_debugging/cppcheck/metadata/D.1__099b4435c38d_meta.json"
d=json.load(open(p))
print("tests", len(d.get("tests", [])))
print("phase", d.get("phase_info"))
print("buggy_fail", [t["test_id"] for t in d.get("tests", []) if t["outcome"] == "FAIL"])
print("fixed_fail", [t["test_id"] for t in d.get("tests", []) if t["outcome_fixed"] == "FAIL"])
print("with_cov", sum(1 for t in d.get("tests", []) if t.get("covered_functions")))
PY'
```
