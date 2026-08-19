# `facebook___rocksdb` - Defects4C x Unified-Debugging

This project uses a dedicated RocksDB image and a project-specific metadata
builder.

## Chuẩn bị input cho Debugging Framework

Thực hiện từ thư mục `/Users/linhnh/Developer/Debugging/defects4c`.

Build image sau mỗi lần thay đổi `Dockerfile.rocksdb` hoặc test adapter:

```bash
docker build -f Dockerfile.rocksdb -t rocksdb/defect4c:latest .
```

Liệt kê 3 defect:

```bash
python3 defectsc_tpl/projects_v1/facebook___rocksdb/run_debugging_case.py \
  --list
```

Prepare một defect:

```bash
python3 defectsc_tpl/projects_v1/facebook___rocksdb/run_debugging_case.py \
  prepare \
  --sha cc8ded6152c51ac853d2915273eed3e6f9af029b \
  --jobs 2 \
  --command-timeout 7200
```

Prepare cả 3 defect, tiếp tục nếu một defect build lỗi hoặc không thỏa oracle:

```bash
python3 defectsc_tpl/projects_v1/facebook___rocksdb/run_debugging_case.py \
  prepare \
  --all \
  --jobs 2 \
  --command-timeout 7200 \
  --continue-on-error
```

Input được tạo tại:

```text
out_tmp_dirs/debugging_framework/rocksdb/inputs/
├── <case-id>/
├── <case-id>.debugging-framework.json
└── <case-id>.failure.log
```

Contract giống LLVM/PHP:

- fixed tree là toàn bộ `commit_after`; buggy tree là fixed tree được overlay
  `files.src` từ `commit_before`;
- chỉ build GoogleTest binary tương ứng với `files.test`, không build/chạy toàn
  bộ RocksDB test suite;
- test khai báo trong `c_compile.test_flags` phải có kết quả
  `FAIL(buggy) -> PASS(fixed)` mới được ghi vào `repair.failing_tests`;
- discover các case trong cùng binary rồi chọn tối đa 70 test bổ sung, ưu tiên
  cùng GoogleTest suite với target;
- cùng tập test bổ sung được chạy trên buggy và fixed; case không `passed` trên
  một trong hai phiên bản được truyền thành `--exclude-test` trong config;
- config dùng `schema_version=6`, ghim OCI image digest, test adapter có bằng
  chứng thực thi và workspace disposable; validation/repair chạy offline.

Kiểm tra hoặc chạy repair một defect đã prepare:

```bash
python3 defectsc_tpl/projects_v1/facebook___rocksdb/run_debugging_case.py \
  doctor --sha cc8ded6152c51ac853d2915273eed3e6f9af029b --jobs 2

python3 defectsc_tpl/projects_v1/facebook___rocksdb/run_debugging_case.py \
  repair --sha cc8ded6152c51ac853d2915273eed3e6f9af029b \
  --jobs 2 --attempts 3 --command-timeout 7200
```

`show --sha <commit_after>` in ra chính xác ba path và lệnh public
Debugging-Framework mà runner sẽ sử dụng. Thêm `--force` cho `prepare` khi
image digest hoặc contract đã thay đổi.

## Pipeline

`build_meta_rocksdb.py` follows the Defects4C convention:

1. Checkout `commit_after`.
2. Overlay `files.src` from `commit_before` to create the buggy tree.
3. Build only the relevant GoogleTest binary from `files.test`.
4. Phase A runs test cases from `c_compile.test_flags` and records buggy
   `outcome`.
5. Phase A fixed checks out `commit_after`, rebuilds the same test binary, and
   records `outcome_fixed`.
6. Phase B checks out buggy again, rebuilds with gcov, runs each selected test
   case one by one, and stores real gcov coverage.
7. `raw/` keeps full gcov coverage. `metadata/` keeps production-only coverage.

There is no coverage fallback. If gcov cannot produce coverage, the test gets
empty `covered_functions` plus `coverage_error`.

Default selection runs all GoogleTest cases in the relevant test binary, at
test-case granularity (`binary::GTestSuite.TestCase`). Phase A and Phase B use
the same selected test-case list for each bug.

## Docker

Build image:

```bash
cd "/Users/linhnh/Documents/Fault Localization/defects4c"
docker build -f Dockerfile.rocksdb -t rocksdb/defect4c:latest .
```

Create container:

```bash
docker rm -f my_defects4c_rocksdb 2>/dev/null || true
docker run -d --name my_defects4c_rocksdb \
  --ipc=host \
  -v "$(pwd)/defectsc_tpl:/src" \
  -v "$(pwd)/out_tmp_dirs:/out" \
  -v "$(pwd)/patche_dirs:/patches" \
  -v "$(pwd)/unified_debugging:/unified_debugging" \
  -v "$(pwd)/../Unified-Debugging:/udbg" \
  rocksdb/defect4c:latest sleep infinity
```

## Run

List bugs:

```bash
docker exec my_defects4c_rocksdb bash -lc '
  cd /src/projects_v1/facebook___rocksdb && \
  python3 build_meta_rocksdb.py --list
'
```

Smoke one bug with only the trigger case and without coverage:

```bash
docker exec my_defects4c_rocksdb bash -lc '
  cd /src/projects_v1/facebook___rocksdb && \
  python3 build_meta_rocksdb.py \
    --sha cc8ded6152c51ac853d2915273eed3e6f9af029b \
    --metadata-dir /out/unified_debugging/rocksdb/metadata \
    --raw-dir /out/unified_debugging/rocksdb/raw \
    --jobs 4 \
    --single-run \
    --skip-coverage \
    --trigger-tests-only \
    --clone
'
```

Generate metadata for all bugs with real coverage:

```bash
docker exec my_defects4c_rocksdb bash -lc '
  cd /src/projects_v1/facebook___rocksdb && \
  python3 build_meta_rocksdb.py \
    --metadata-dir /out/unified_debugging/rocksdb/metadata \
    --raw-dir /out/unified_debugging/rocksdb/raw \
    --jobs 4 \
    --skip-if-exists \
    --clone
'
```

## Metadata

Important fields:

| Field | Meaning |
|---|---|
| `tests[*].test_id` | `test_binary::GTestSuite.TestCase`. |
| `tests[*].outcome` | Buggy outcome. |
| `tests[*].outcome_fixed` | Fixed outcome when dual run is enabled. |
| `tests[*].covered_functions` | Real gcov function coverage from buggy Phase B. |
| `compile_cmd` | Rebuilds selected RocksDB test binary for APR validation. |
| `test_cmd_template` | `bash <repo>/run_one_test.sh {test_id}`. |

`run_one_test.sh` is generic: it finds the selected test binary under
`build_meta_rocksdb` and runs the requested GoogleTest filter.

Sau đó kill đúng PID trong container:

```bash
docker exec my_defects4c_rocksdb bash -lc 'kill <pid>'
```
