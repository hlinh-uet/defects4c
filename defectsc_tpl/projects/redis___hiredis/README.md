# `redis___hiredis` - Defects4C x Unified-Debugging

## 1. Project Data

Input files in this folder:

| File | Role |
|---|---|
| `bugs_list_new.json` | One CVE bug: `CVE-2021-32765`. `type.id` is the `bug_id`. |
| `project.json` | Original Defects4C project config for `https://github.com/redis/hiredis`. |
| `build_tpl.jinja` | Original CMake + Ninja build template. |
| `test_tpl.jinja` | Original `ctest`/custom output oracle. |
| `build_meta_hiredis.py` | Metadata builder for Unified-Debugging. |

Output defaults:

| Output | Host path |
|---|---|
| Metadata | `defects4c/out_tmp_dirs/unified_debugging/hiredis/metadata/{bug_id}_meta.json` |
| Raw | `defects4c/out_tmp_dirs/unified_debugging/hiredis/raw/{bug_id}_meta.json` |
| Repo clone | `defects4c/out_tmp_dirs/redis___hiredis/git_repo_dir_{bug_id}` |

The metadata and raw JSON contents are intentionally identical.

## 2. Build Flow

For each bug:

1. Checkout fixed tree at `commit_after`.
2. Create buggy tree by keeping `HEAD` at `commit_after` and overlaying only `files.src` from `commit_before`.
3. Phase A builds with ASAN and runs the hiredis custom test suite on buggy.
4. Phase A checks out fixed, rebuilds with ASAN, and reruns the same suite to fill `outcome_fixed`.
5. The script parses numbered output lines such as `#27 ... PASSED` into individual test entries.
6. Phase B checks out buggy again, rebuilds with GCOV and no ASAN, and records `covered_functions`.

The builder is intentionally strict: it does not infer coverage from ASAN
stack traces or stderr. If Phase A fixed, Phase B GCOV, or the final ASAN
rebuild fails, the output record is marked with `build_error` instead of
silently writing metadata that looks complete but is not trustworthy.

The script asserts these checkout invariants before every build:

| Tree | Required state |
|---|---|
| Fixed | `HEAD == commit_after`, `files.src` and `files.test` match `commit_after`. |
| Buggy | `HEAD == commit_after`, `files.src` matches `commit_before`, `files.test` still matches `commit_after`. |

Expected related test for the current bug:

| bug_id | buggy outcome | fixed outcome | runner |
|---|---|---|---|
| `CVE-2021-32765` | `FAIL` | `PASS` | `hiredis_038_Multi-bulk_never_overflows_regardless_of_maxelements` |

The ground-truth function should be extracted as `createArrayObject` from the
patch hunk in `hiredis.c`.

Note: hiredis has one custom test executable and no native per-case filter. The builder stores each numbered test line as a separate metadata test, but Phase B coverage is collected at suite granularity and reused for each custom test entry. For buggy Phase A, the builder first runs strict ASAN to catch the CVE oracle, then reruns tolerant ASAN to recover the full numbered test list and overlays the sanitizer failure on the maxelements test.

## 3. Docker

Run from `defects4c/`:

```bash
docker build -f Dockerfile.hiredis -t hiredis/defect4c:latest .

docker rm -f my_defects4c_hiredis 2>/dev/null || true
docker run -d --name my_defects4c_hiredis \
  --ipc=host \
  -v "$(pwd)/defectsc_tpl:/src" \
  -v "$(pwd)/out_tmp_dirs:/out" \
  -v "$(pwd)/patche_dirs:/patches" \
  -v "$(pwd)/../Unified-Debugging:/udbg" \
  hiredis/defect4c:latest sleep infinity
```

## 4. Run One Bug

Prepare/clone the repo:

```bash
docker exec my_defects4c_hiredis bash -lc '
  cd /src/projects/redis___hiredis && \
  python3 build_meta_hiredis.py \
    --prepare-repos \
    --clone \
    --sha 76a7b10005c70babee357a7d0f2becf28ec7ed1e
'
```

Generate metadata:

```bash
docker exec my_defects4c_hiredis bash -lc '
  cd /src/projects/redis___hiredis && \
  python3 build_meta_hiredis.py \
    --sha 76a7b10005c70babee357a7d0f2becf28ec7ed1e \
    --metadata-dir /out/unified_debugging/hiredis/metadata \
    --raw-dir /out/unified_debugging/hiredis/raw \
    --dual-run \
    --gcov-scope all
'
```

Run all matching bugs with resume:

```bash
docker exec my_defects4c_hiredis bash -lc '
  cd /src/projects/redis___hiredis && \
  python3 build_meta_hiredis.py \
    --metadata-dir /out/unified_debugging/hiredis/metadata \
    --raw-dir /out/unified_debugging/hiredis/raw \
    --dual-run \
    --gcov-scope all \
    --skip-if-exists
'
```

## 5. Verify

Quick checks on host:

```powershell
$m = Get-Content defects4c\out_tmp_dirs\unified_debugging\hiredis\metadata\CVE-2021-32765_meta.json -Raw | ConvertFrom-Json
$m.tests.Count
$m.phase_info
$m.tests | Where-Object { $_.outcome -eq 'FAIL' -and $_.outcome_fixed -eq 'PASS' } | Select test_id,outcome,outcome_fixed,fail_reason
($m.tests | Where-Object { $_.covered_functions.Count -gt 0 }).Count
```

Expected:

| Check | Expected |
|---|---|
| `bug_id` | `CVE-2021-32765` |
| `outcome=FAIL`, `outcome_fixed=PASS` | Only the maxelements multi-bulk test |
| `phase_a_fixed_fail_count` | `0` |
| `phase_b_with_coverage` | Same as `tests.Count` when GCOV succeeds; otherwise the record has `build_error` |
| `raw` and `metadata` | Same JSON content |
