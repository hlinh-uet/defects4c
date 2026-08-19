# mongo-c-driver: prepare input cho Debugging Framework

Chạy các lệnh từ thư mục:

```bash
cd /Users/linhnh/Developer/Debugging/defects4c
```

## Contract

Dataset hiện có một record: `CVE-2018-16790`. Target native được ánh xạ từ
`src/libbson/tests/binary/test59.bson` sang test ID `/bson/validate` của
`test-libbson`.

Pipeline tuân theo cùng contract với LLVM, PHP, SPIRV-Tools, tcpdump và curl:

- fixed là toàn bộ cây `commit_after`;
- buggy là fixed tree và chỉ overlay `files.src` từ `commit_before`;
- chỉ target có kết quả `FAIL(buggy) -> PASS(fixed)` được ghi vào
  `repair.failing_tests`;
- ngoài target, chọn ổn định tối đa 70 test ID libbson đã đăng ký trong mã
  nguồn, rồi chạy đúng cùng tập đó trên buggy và fixed;
- test bổ sung không pass trên cả buggy và fixed vẫn thuộc tập đã chọn, nhưng
  được ghi bằng `--exclude-test` trong regression command;
- config dùng `schema_version=6`, ghim OCI image digest, chạy offline và dùng
  disposable workspace;
- input cuối được materialize lại sạch, không chứa `.git` hoặc build artifact
  từ bước prepare.

## Build image

Build lại image sau khi thay đổi `Dockerfile.mongo-c-driver`,
`run_mongo_c_driver_build.py` hoặc `run_mongo_c_driver_tests.py`:

```bash
docker build \
  -f Dockerfile.mongo-c-driver \
  -t mongo-c-driver/defect4c:latest \
  .
```

## Liệt kê và prepare

```bash
python3 \
  defectsc_tpl/projects/mongodb___mongo-c-driver/run_debugging_case.py \
  --list
```

Prepare case duy nhất:

```bash
python3 \
  defectsc_tpl/projects/mongodb___mongo-c-driver/run_debugging_case.py \
  prepare \
  --sha 0d9a4d98bfdf4acd2c0138d4aaeb4e2e0934bd84 \
  --jobs 2 \
  --command-timeout 7200
```

Có thể dùng `--all`; với dataset hiện tại lệnh này cũng chỉ chạy một case:

```bash
python3 \
  defectsc_tpl/projects/mongodb___mongo-c-driver/run_debugging_case.py \
  prepare \
  --all \
  --jobs 2 \
  --command-timeout 7200 \
  --continue-on-error
```

Thêm `--force` nếu cần tạo lại input sau khi image hoặc contract thay đổi.
Output nằm tại:

```text
out_tmp_dirs/debugging_framework/mongo-c-driver/inputs/
├── CVE-2018-16790__0d9a4d98bfdf/
├── CVE-2018-16790__0d9a4d98bfdf.debugging-framework.json
└── CVE-2018-16790__0d9a4d98bfdf.failure.log
```

## Doctor và repair

Xem lệnh Framework đã được resolve:

```bash
python3 \
  defectsc_tpl/projects/mongodb___mongo-c-driver/run_debugging_case.py \
  show \
  --sha 0d9a4d98bfdf4acd2c0138d4aaeb4e2e0934bd84 \
  --jobs 2
```

Chạy doctor:

```bash
python3 \
  defectsc_tpl/projects/mongodb___mongo-c-driver/run_debugging_case.py \
  doctor \
  --sha 0d9a4d98bfdf4acd2c0138d4aaeb4e2e0934bd84 \
  --jobs 2 \
  --command-timeout 7200
```

Chạy repair:

```bash
python3 \
  defectsc_tpl/projects/mongodb___mongo-c-driver/run_debugging_case.py \
  repair \
  --sha 0d9a4d98bfdf4acd2c0138d4aaeb4e2e0934bd84 \
  --jobs 2 \
  --command-timeout 7200
```
