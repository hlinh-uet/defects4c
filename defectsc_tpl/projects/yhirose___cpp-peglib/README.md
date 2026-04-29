# `yhirose___cpp-peglib` - Defects4C x Unified-Debugging

## 1. Project data

Input files in this folder:

| File | Role |
|---|---|
| `bugs_list_new.json` | One CVE bug: `CVE-2020-23915`. `type.id` is the `bug_id`. |
| `project.json` | Original Defects4C project config for `https://github.com/yhirose/cpp-peglib`. |
| `build_tpl.jinja` | Original CMake + Ninja build template. |
| `test_tpl.jinja` | Original runner for `build_dir/test/test-main`. |
| `build_meta_peglib.py` | Metadata builder for Unified-Debugging. |

Output defaults:

| Output | Host path |
|---|---|
| Metadata | `defects4c/out_tmp_dirs/unified_debugging/peglib/metadata/{bug_id}_meta.json` |
| Raw | `defects4c/out_tmp_dirs/unified_debugging/peglib/raw/{bug_id}_meta.json` |
| Repo clone | `defects4c/out_tmp_dirs/yhirose___cpp-peglib/git_repo_dir_{bug_id}` |

The metadata and raw JSON contents are intentionally identical.

## 2. Build flow

For each bug:

1. Checkout fixed tree at `commit_after`.
2. Create buggy tree by overlaying only `files.src` from `commit_before`.
3. Phase A builds with ASAN and runs `test/test-main` on buggy.
4. Phase A checks out fixed, rebuilds with ASAN, and reruns the same test to fill `outcome_fixed`.
5. Phase B checks out buggy again, rebuilds with GCOV and no ASAN, and records `covered_functions`.
6. The script rebuilds buggy ASAN at the end so `test_cmd_template` can reproduce the failing oracle.

Expected related test for the current bug:

| bug_id | buggy outcome | fixed outcome | runner |
|---|---|---|---|
| `CVE-2020-23915` | `FAIL` | `PASS` | `test-main` built from `test/test1.cc` |

The ground-truth function should be extracted as `codepoint_length` from the
patch hunk in `peglib.h`.

## 3. Docker

Run from `defects4c/`:

```bash
docker build -f Dockerfile.peglib -t peglib/defect4c:latest .

docker rm -f my_defects4c_peglib 2>/dev/null || true
docker run -d --name my_defects4c_peglib \
  --ipc=host \
  -v "$(pwd)/defectsc_tpl:/src" \
  -v "$(pwd)/out_tmp_dirs:/out" \
  -v "$(pwd)/patche_dirs:/patches" \
  -v "$(pwd)/../Unified-Debugging:/udbg" \
  peglib/defect4c:latest sleep infinity
```

## 4. Run one bug

Copy the bug list into metadata before verification:

```bash
mkdir -p out_tmp_dirs/unified_debugging/peglib/metadata
cp defectsc_tpl/projects/yhirose___cpp-peglib/bugs_list_new.json \
  out_tmp_dirs/unified_debugging/peglib/metadata/bugs_list_new.json
```

Prepare/clone the repo:

```bash
docker exec my_defects4c_peglib bash -lc '
  cd /src/projects/yhirose___cpp-peglib && \
  python3 build_meta_peglib.py \
    --prepare-repos \
    --clone \
    --sha b3b29ce8f3acf3a32733d930105a17d7b0ba347e
'
```

Generate metadata:

```bash
docker exec my_defects4c_peglib bash -lc '
  cd /src/projects/yhirose___cpp-peglib && \
  python3 build_meta_peglib.py \
    --sha b3b29ce8f3acf3a32733d930105a17d7b0ba347e \
    --metadata-dir /out/unified_debugging/peglib/metadata \
    --raw-dir /out/unified_debugging/peglib/raw \
    --dual-run \
    --gcov-scope all
'
```

Run all matching bugs with resume:

```bash
docker exec my_defects4c_peglib bash -lc '
  cd /src/projects/yhirose___cpp-peglib && \
  python3 build_meta_peglib.py \
    --metadata-dir /out/unified_debugging/peglib/metadata \
    --raw-dir /out/unified_debugging/peglib/raw \
    --dual-run \
    --gcov-scope all \
    --skip-if-exists
'
```

## 5. Verify

After the first run, inspect:

```bash
jq '.bug_id, .ground_truth_functions, .phase_info, .tests[] | {test_id, outcome, outcome_fixed, covered_count: (.covered_functions | length)}' \
  out_tmp_dirs/unified_debugging/peglib/metadata/CVE-2020-23915_meta.json
```

Checklist:

| Check | Expected |
|---|---|
| `bug_id` | `CVE-2020-23915` |
| `tests[0].outcome` | `FAIL` |
| `tests[0].outcome_fixed` | `PASS` |
| `tests[0].covered_functions` | Non-empty, preferably includes `peglib.h:*` functions |
| `ground_truth_functions` | Includes `codepoint_length` |
| `test_cmd_template` | Points to `/out/yhirose___cpp-peglib/git_repo_dir_CVE-2020-23915/run_one_test.sh {test_id}` |

## 6. Debug artifacts

When the result is not as expected, rerun with `--debug-artifacts`:

```bash
docker exec my_defects4c_peglib bash -lc '
  cd /src/projects/yhirose___cpp-peglib && \
  python3 build_meta_peglib.py \
    --sha b3b29ce8f3acf3a32733d930105a17d7b0ba347e \
    --metadata-dir /out/unified_debugging/peglib/metadata \
    --raw-dir /out/unified_debugging/peglib/raw \
    --dual-run \
    --gcov-scope all \
    --debug-artifacts
'
```

Logs are written to:

```text
out_tmp_dirs/unified_debugging/peglib/debug/CVE-2020-23915/
```

Important files:

| File | What to check |
|---|---|
| `00_bug.json` | Commit IDs, source/test files, compile command. |
| `phaseA-buggy/01_cmake_configure.log` | Actual CMake command and flags. |
| `phaseA-buggy/02_cmake_build.log` | Compile/link errors and sanitizer flags. |
| `phaseA-buggy/*test.log` | Exact test command, exit code, stdout/stderr. |
| `phaseA-buggy/*summary.json` | Script's PASS/FAIL decision and output tail. |
| `phaseB/*gcov*.log` | Raw `gcov` errors/output when coverage is empty. |
| `phaseB/*coverage.json` | Parsed coverage map before it is converted to `covered_functions`. |

Manual rerun inside the container:

```bash
docker exec my_defects4c_peglib bash -lc '
  cd /out/yhirose___cpp-peglib/git_repo_dir_CVE-2020-23915 && \
  bash ./run_one_test.sh TestMain; echo rc=$?
'
```

If `covered_functions` is empty, rerun Phase B with the full scope:

```bash
docker exec my_defects4c_peglib bash -lc '
  cd /src/projects/yhirose___cpp-peglib && \
  python3 build_meta_peglib.py \
    --sha b3b29ce8f3acf3a32733d930105a17d7b0ba347e \
    --metadata-dir /out/unified_debugging/peglib/metadata \
    --raw-dir /out/unified_debugging/peglib/raw \
    --dual-run \
    --gcov-scope all
'
```

Zip shared data after verification:

```bash
cd out_tmp_dirs/unified_debugging
zip -r peglib.zip peglib
```
