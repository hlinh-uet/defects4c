# `CESNET___libyang` — Defects4C × Unified-Debugging

## 1. Kiến trúc

```
defects4c/
├── Dockerfile.libyang                              (★ image SLIM cho libyang)
├── defectsc_tpl/
│   └── projects_v1/
│       └── CESNET___libyang/
│           ├── bugs_list_new.json                  (15 bug entries)
│           ├── project.json                        (khai báo build: cmake/ninja/ctest)
│           ├── build_meta_libyang.py               (★ pipeline sinh metadata)
│           └── README.md                           (file này)
├── out_tmp_dirs/
│   └── CESNET___libyang/
│       └── git_repo_dir_<sha>/                     (cây nguồn đã clone)
└── unified_debugging/
    └── libyang/
        ├── metadata/                               (output cho Unified-Debugging)
        └── raw/                                    (output thô)
```

## 2. Khác biệt so với tcpdump

| Tiêu chí | tcpdump | libyang |
|---|---|---|
| Build system | autoconf + make | **CMake + Ninja** |
| Test runner | `tests/TESTLIST` + `TESTonce` | **CTest** (CMocka unit tests) |
| Dependencies | libpcap-dev | **libpcre2-dev, libcmocka-dev** |
| Test discovery | Parse `TESTLIST` | `ctest --show-only=json-v1`, rồi expand CMocka cases |
| Test filter | `bugs_list_new.json` không có test_flags | Mặc định chạy full CTest; có thể dùng `--trigger-tests-only` để debug nhanh |

## 3. Pipeline `build_meta_libyang.py`

Với mỗi bug trong `bugs_list_new.json`:

1. **Checkout buggy**: fixed tree (`commit_after`) + overlay `src_files` từ `commit_before`
2. **CMake configure**: `cmake -G Ninja -S . -B build_meta_libyang -DCMAKE_C_FLAGS="-g -O0 -fprofile-arcs -ftest-coverage" -DENABLE_TESTS=ON`
3. **Ninja build**: `ninja -C build_meta_libyang -jN`
4. **Test discovery**: `ctest --test-dir build_meta_libyang --show-only=json-v1`, rồi parse `cmocka_unit_test*()`/`UTEST()` trong source để tạo từng `CTestName::case_name`
5. **Phase A buggy**: chạy từng CMocka test case tuần tự để lấy `tests[*].outcome`
6. **Phase A fixed**: checkout fixed tree, build lại, chạy cùng danh sách test case để lấy `tests[*].outcome_fixed`
7. **Phase B coverage**: checkout buggy tree, build lại, chạy từng test case tuần tự; trước mỗi case xóa `.gcda`, sau case đọc coverage bằng `gcov`
8. **Ghi metadata**: `{safe_bug_id}_meta.json` vào `raw/` và `metadata/`

Script có lock theo `out-root` trong `/tmp`, nên chỉ một process được dùng cùng cây repo/output libyang tại một thời điểm. Có thể dùng `--single-run` hoặc `--trigger-tests-only` nếu cần chạy nhanh để debug.

`--jobs 4` chỉ là số job song song khi build bằng Ninja (`ninja -j4`). Nó không chạy 4 pipeline metadata song song và không chạy 4 test coverage song song. Test vẫn chạy tuần tự từng case để tránh `.gcda` bị ghi đè.

Coverage không dùng fallback từ ASAN stack trace hay nguồn suy luận khác. Nếu `gcov` không có, không có `.gcda`, hoặc parse không ra function, test đó sẽ có `coverage_error` trong metadata và `covered_functions` để rỗng.

Libyang không expose CLI filter cho từng CMocka case. Script áp dụng một patch build-only sau mỗi checkout để biến env `BUILD_META_CMOCKA_TEST_FILTER` thành `cmocka_set_test_filter()`. Patch này chỉ nằm trong working tree tạm của metadata run và được áp dụng nhất quán cho buggy/fixed/coverage builds.

`--skip-if-exists` chỉ skip metadata đã sinh bằng logic case-level hiện tại (`phase_info.test_granularity == "cmocka_case"`). Metadata CTest-level cũ sẽ được phát hiện và chạy lại.

## 4. Docker Setup

### Build image
```bash
cd "/Users/linhnh/Documents/Fault Localization/defects4c"
docker build -f Dockerfile.libyang -t libyang/defect4c:latest .
```

### Tạo container
```bash
docker rm -f my_defects4c_libyang 2>/dev/null || true
docker run -d --name my_defects4c_libyang \
  --ipc=host \
  -v "$(pwd)/defectsc_tpl:/src" \
  -v "$(pwd)/out_tmp_dirs:/out" \
  -v "$(pwd)/patche_dirs:/patches" \
  -v "$(pwd)/unified_debugging:/unified_debugging" \
  -v "$(pwd)/../Unified-Debugging:/udbg" \
  libyang/defect4c:latest sleep infinity
```

### Clone repos (chạy 1 lần)
```bash
docker exec my_defects4c_libyang bash -lc \
  'cd /src && bash bulk_git_clone_v2.sh mini CESNET___libyang'
```

## 5. Chạy pipeline

### Chạy thử 1 bug
```bash
docker exec my_defects4c_libyang bash -lc '
  cd /src/projects_v1/CESNET___libyang && \
  python3 build_meta_libyang.py \
    --sha 92cc8517fcb85dcfbb93842758571f721c49c9cb \
    --metadata-dir /out/unified_debugging/libyang/metadata \
    --raw-dir /out/unified_debugging/libyang/raw \
    --jobs 4
'
```

Lệnh trên mặc định chạy full CTest cho bug đó, gồm buggy outcome, fixed outcome và coverage trên buggy tree.

### Chạy toàn bộ
```bash
docker exec my_defects4c_libyang bash -lc '
  cd /src/projects_v1/CESNET___libyang && \
  python3 build_meta_libyang.py \
    --metadata-dir /out/unified_debugging/libyang/metadata \
    --raw-dir /out/unified_debugging/libyang/raw \
    --jobs 4 \
    --skip-if-exists \
    --clone
'
```

Nếu muốn ghi lại toàn bộ output đã có, bỏ `--skip-if-exists`.

### Debug nhanh bằng trigger test
```bash
docker exec my_defects4c_libyang bash -lc '
  cd /src/projects_v1/CESNET___libyang && \
  python3 build_meta_libyang.py \
    --sha 92cc8517fcb85dcfbb93842758571f721c49c9cb \
    --metadata-dir /out/unified_debugging/libyang/metadata \
    --raw-dir /out/unified_debugging/libyang/raw \
    --jobs 4 \
    --trigger-tests-only
'
```

## 6. Mapping Unified-Debugging

| Unified-Debugging field | Cách đáp ứng |
|---|---|
| `bug_id` | `type.id` trong `bugs_list_new.json` |
| `dataset_name` | `"defects4c"` |
| `language` | `"C"` |
| `source_file` | `<git_repo_dir>/<files.src[0]>` |
| `compile_cmd` | `cmake + ninja` command |
| `test_cmd_template` | `bash <repo>/run_one_test.sh {test_id}` |
| `tests[*].test_id` | CMocka case id dạng `CTestName::case_name` (e.g. `utest_new::test_dup`) |
| `tests[*].outcome` | `PASS/FAIL` buggy version |
| `tests[*].outcome_fixed` | `PASS/FAIL` fixed version; `NOT_RUN` chỉ khi dùng `--single-run` hoặc fixed phase lỗi |
| `tests[*].covered_functions` | `gcov` parse thật → `"file.c:func"` với basename của source file |
| `tests[*].coverage_error` | Chỉ xuất hiện khi coverage của test đó không thu được |
| `phase_info.errors` | Lỗi cấp phase như `fixed_compile_failed`, `coverage_compile_failed`, `no_tests_selected` |
| `ground_truth_functions` | Parse hunk header `git diff` |

## 7. Kiểm tra output

Ví dụ kiểm tra một metadata đã sinh:

```bash
docker exec my_defects4c_libyang bash -lc 'python3 - <<'"'"'PY'"'"'
import json
p="/out/unified_debugging/libyang/metadata/D.1__92cc8517fcb8_meta.json"
d=json.load(open(p))
print("tests", len(d["tests"]))
print("missing_fixed", sum(1 for t in d["tests"] if not t.get("outcome_fixed")))
print("coverage_errors", [(t["test_id"], t.get("coverage_error")) for t in d["tests"] if t.get("coverage_error")])
print("phase_errors", d.get("phase_info", {}).get("errors"))
print("buggy_fail", [t["test_id"] for t in d["tests"] if t["outcome"] == "FAIL"])
print("fixed_fail", [t["test_id"] for t in d["tests"] if t["outcome_fixed"] == "FAIL"])
PY'
```

Lưu ý: nếu full suite có test fixed vẫn FAIL thì đó là kết quả thật của fixed tree và vẫn được lưu trong `outcome_fixed`; pipeline không tự lọc chỉ test regression.


Nếu cần dừng run cũ rồi chạy lại, kiểm tra PID trong container libyang:

```bash
docker exec my_defects4c_libyang bash -lc \
  'ps -eo pid,ppid,stat,cmd | grep "[p]ython3 build_meta_libyang.py" || true'
```

Sau đó kill đúng PID trong container libyang:

```bash
docker exec my_defects4c_libyang bash -lc 'kill <pid>'
```
