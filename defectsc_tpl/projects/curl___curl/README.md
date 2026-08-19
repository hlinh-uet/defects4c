# curl: prepare input cho Debugging Framework

Chạy các lệnh từ thư mục:

```bash
cd /Users/linhnh/Developer/Debugging/defects4c
```

## Contract

Dataset hiện có một record: `CVE-2017-7407`, với hai target native curl là
`1440` và `1441`.

Pipeline áp dụng cùng contract với LLVM, PHP, SPIRV-Tools và tcpdump:

- fixed là toàn bộ cây `commit_after`;
- buggy là fixed tree và chỉ overlay `files.src` từ `commit_before`;
- target được ánh xạ từ `files.test` sang số test thật của
  `tests/runtests.pl`;
- chỉ target có kết quả `FAIL(buggy) -> PASS(fixed)` được ghi vào
  `repair.failing_tests`;
- ngoài target, chọn ổn định tối đa 70 file `tests/data/testN`, rồi chạy đúng
  cùng tập đó trên buggy và fixed;
- regression test không pass trên cả hai phía được ghi bằng `--exclude-test`;
- config dùng `schema_version=6`, ghim OCI image digest, chạy offline và dùng
  disposable workspace;
- input cuối được materialize lại sạch, không chứa `.git` hoặc build artifact
  từ bước prepare.

## Build image

Build lại image khi thay đổi `Dockerfile.curl`, `run_curl_build.py` hoặc
`run_curl_tests.py`:

```bash
docker build -f Dockerfile.curl -t curl/defect4c:latest .
```

## Liệt kê và prepare

```bash
python3 \
  defectsc_tpl/projects/curl___curl/run_debugging_case.py \
  --list
```

Prepare case duy nhất:

```bash
python3 \
  defectsc_tpl/projects/curl___curl/run_debugging_case.py \
  prepare \
  --sha 1890d59905414ab84a35892b2e45833654aa5c13 \
  --jobs 2 \
  --command-timeout 7200
```

Có thể dùng `--all`; với dataset hiện tại lệnh này cũng chỉ chạy một case:

```bash
python3 \
  defectsc_tpl/projects/curl___curl/run_debugging_case.py \
  prepare \
  --all \
  --jobs 2 \
  --command-timeout 7200 \
  --continue-on-error
```

Thêm `--force` nếu cần tạo lại input sau khi image hoặc contract thay đổi.
Output nằm tại:

```text
out_tmp_dirs/debugging_framework/curl/inputs/
├── CVE-2017-7407__1890d5990541/
├── CVE-2017-7407__1890d5990541.debugging-framework.json
└── CVE-2017-7407__1890d5990541.failure.log
```

## Doctor và repair

Xem lệnh Framework đã được resolve:

```bash
python3 \
  defectsc_tpl/projects/curl___curl/run_debugging_case.py \
  show \
  --sha 1890d59905414ab84a35892b2e45833654aa5c13 \
  --jobs 2
```

Chạy doctor:

```bash
python3 \
  defectsc_tpl/projects/curl___curl/run_debugging_case.py \
  doctor \
  --sha 1890d59905414ab84a35892b2e45833654aa5c13 \
  --jobs 2 \
  --command-timeout 7200
```

Chạy repair:

```bash
python3 \
  defectsc_tpl/projects/curl___curl/run_debugging_case.py \
  repair \
  --sha 1890d59905414ab84a35892b2e45833654aa5c13 \
  --jobs 2 \
  --command-timeout 7200
```
