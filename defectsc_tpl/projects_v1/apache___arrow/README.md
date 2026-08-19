# Apache Arrow: prepare input cho Debugging Framework

Chạy các lệnh từ thư mục:

```bash
cd /Users/linhnh/Developer/Debugging/defects4c
```

## Dataset và contract

Dataset có 9 record. Mỗi record khai báo một CTest/GoogleTest target trong
`unittest.name`, ví dụ `arrow-concatenate-test`.

Pipeline tuân theo cùng contract với LLVM, PHP, SPIRV-Tools, RocksDB, tcpdump,
curl và mongo-c-driver:

- fixed là toàn bộ cây `commit_after`;
- buggy là fixed tree và chỉ overlay `files.src` từ `commit_before`;
- hai submodule test-data `apache/arrow-testing` và `apache/parquet-testing`
  được materialize tại đúng gitlink của `commit_after`, rồi giữ trong input để
  validation không cần mạng;
- build chỉ target được khai báo cho bug, không build toàn bộ Arrow;
- chạy target buggy ở GoogleTest-case granularity và chỉ ghi case có kết quả
  `FAIL(buggy) -> PASS(fixed)` vào `repair.failing_tests`;
- ngoài case lỗi, chọn ổn định tối đa 70 case khác trong cùng target và chạy
  đúng cùng tập đó trên buggy và fixed;
- case bổ sung không pass trên cả hai phía vẫn thuộc tập đã chọn nhưng được ghi
  bằng `--exclude-test` trong regression command;
- config dùng `schema_version=6`, ghim OCI image digest, chạy offline và dùng
  disposable workspace;
- build artifact bị loại khỏi input cuối. Source submodule test-data được giữ
  lại vì là dependency runtime, không phải output build.

Hai record cùng có bug ID `A.2`; vì vậy nên chọn bằng SHA thay vì `--bug A.2`.

## Build image

```bash
docker build \
  -f Dockerfile.arrow \
  -t arrow/defect4c:latest \
  .
```

Image dùng system dependency để validation offline. Riêng source archive Apache
ORC 1.8.0 được ghim checksum và đóng trong image cho target
`arrow-orc-adapter-test`.

## Liệt kê và prepare

```bash
python3 \
  defectsc_tpl/projects_v1/apache___arrow/run_debugging_case.py \
  --list
```

Prepare thử record đầu tiên:

```bash
python3 \
  defectsc_tpl/projects_v1/apache___arrow/run_debugging_case.py \
  prepare \
  --sha 68e0fa7499876fc0cf86b8be784a890226648645 \
  --jobs 2 \
  --command-timeout 7200
```

Prepare toàn bộ 9 record:

```bash
python3 \
  defectsc_tpl/projects_v1/apache___arrow/run_debugging_case.py \
  prepare \
  --all \
  --jobs 2 \
  --command-timeout 7200 \
  --continue-on-error
```

Thêm `--force` nếu cần materialize lại sau khi image hoặc contract thay đổi.
Output nằm tại:

```text
out_tmp_dirs/debugging_framework/arrow/inputs/
├── <case-id>/
├── <case-id>.debugging-framework.json
└── <case-id>.failure.log
```

## Doctor và repair

```bash
python3 \
  defectsc_tpl/projects_v1/apache___arrow/run_debugging_case.py \
  doctor \
  --sha 68e0fa7499876fc0cf86b8be784a890226648645 \
  --jobs 2

python3 \
  defectsc_tpl/projects_v1/apache___arrow/run_debugging_case.py \
  repair \
  --sha 68e0fa7499876fc0cf86b8be784a890226648645 \
  --jobs 2 \
  --attempts 2 \
  --command-timeout 7200 \
  --codex-timeout 7200
```
