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
- chạy target buggy để tìm case lỗi, rồi chạy riêng từng case bằng đúng command
  Framework sẽ dùng; chỉ ghi case đạt `FAIL(buggy, chạy riêng) -> PASS(fixed)`
  vào `repair.failing_tests`. Case chỉ fail khi chạy chung target bị loại khỏi
  defect oracle và ghi rõ kết quả trong manifest;
- ngoài case lỗi, chọn ổn định tối đa 70 case khác trong cùng target và chạy
  đúng cùng tập đó trên buggy và fixed;
- case bổ sung không pass trên cả hai phía vẫn thuộc tập đã chọn nhưng được ghi
  bằng `--exclude-test` trong regression command;
- config dùng contract canonical, ghim OCI image digest, chạy offline và dùng
  disposable workspace;
- build artifact bị loại khỏi input cuối. Source submodule test-data được giữ
  lại vì là dependency runtime, không phải output build.

Hai record cùng có bug ID `A.2`; vì vậy nên chọn bằng SHA thay vì `--bug A.2`.

## Build image

Tải trước source ORC 1.8.0 trên host (Docker build network có thể không truy
cập được `archive.apache.org`); Dockerfile copy file này và kiểm tra checksum:

```bash
mkdir -p docker_deps
curl -fsSL https://archive.apache.org/dist/orc/orc-1.8.0/orc-1.8.0.tar.gz \
  -o docker_deps/orc-1.8.0.tar.gz
```

```bash
docker build \
  -f Dockerfile.arrow \
  -t arrow/defect4c:latest \
  .
```

Image dùng system dependency để validation offline. Riêng source archive Apache
ORC 1.8.0 được ghim checksum và đóng trong image cho target
`arrow-orc-adapter-test`.

Target ORC cần cả `libprotobuf-dev`, `libprotoc-dev` và `protobuf-compiler` để
CMake tìm được thư viện `libprotoc`. Image đặt `ZLIB_HOME=/usr` và
`ZSTD_HOME=/usr` để ORC dùng thư viện có sẵn, tránh tự tải dependency khi build
offline. Build Debug dùng thêm
`BUILD_WARNING_LEVEL=PRODUCTION` cho các bản Arrow cũ không hỗ trợ
`ARROW_USE_WERROR=OFF`, tránh warning của compiler mới làm dừng build.
Riêng target ORC dùng Arrow static để không đăng ký trùng Protobuf descriptor
khi test link `liborc.a` trực tiếp; các target còn lại tiếp tục dùng Arrow shared.

Image build GoogleTest/GoogleMock shared từ source `/usr/src/googletest` do
Ubuntu cung cấp, cài tại `/opt/arrow-googletest`. Runner và config đã xuất ghim
`GTest_DIR=/opt/arrow-googletest/lib/cmake/GTest`, tránh lấy nhầm các archive
static trong `/usr/lib`. Cách này khắc phục double-free của global
`testing::FLAGS_gtest_filter` khi executable và `libarrow_testing.so` cùng nhúng
GoogleTest static, đặc biệt khi chạy riêng test của case B. Build bước này dùng
source có sẵn trong image và không tải thêm GoogleTest từ mạng.

Build lại image sau khi cập nhật `run_arrow_tests.py` để adapter trong image
giữ stdout/stderr của cả test pass lẫn test fail và thu thập artifact từng test.

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

Chỉ prepare lại ba case D.2, C.2 và A.3 sau khi build image mới:

```bash
python3 defectsc_tpl/projects_v1/apache___arrow/run_debugging_case.py prepare \
  --sha eea4a54f66a3 --sha 3d0a9d58b6fe --sha c4f8436e2532 \
  --jobs 4 --command-timeout 7200 --continue-on-error --force
```

Prepare case B sau khi build image có GoogleTest shared:

```bash
python3 defectsc_tpl/projects_v1/apache___arrow/run_debugging_case.py prepare \
  --sha 912e2bb33455 --jobs 4 --command-timeout 7200
```

Thêm `--force` nếu cần materialize lại sau khi image hoặc contract thay đổi.
Input cũ được tái sử dụng mà không chạy verification lại; muốn áp dụng điều
kiện fail khi chạy riêng và tạo log/manifest mới cho input cũ, cần dùng `--force`.
Ba input chỉ được publish sau khi verification và materialize source sạch
thành công. Output nằm tại:

```text
out_tmp_dirs/debugging_framework/arrow/inputs/
├── <case-id>/
├── <case-id>.debugging-framework.json
└── <case-id>.failure.log
```

Log chi tiết được giữ riêng cho mỗi lần prepare thực sự, kể cả khi lỗi hoặc
timeout; retry và `--force` không ghi đè log của lần trước:

```text
out_tmp_dirs/debugging_framework/arrow/prepare-logs/<case-id>/<UTC-timestamp>-<unique-id>/
├── manifest.json
├── source-cache.log                 # Khi cache cần clone/fetch
├── dependency-sync.log
├── buggy-build.log
├── buggy-target-tests.log           # Cả target và từng case chạy riêng
├── test-discovery.log
├── buggy-regression-tests.log
├── fixed-verification.log           # Build fixed, target và regression
├── test-artifacts/
│   ├── buggy-target-tests/<snapshot>/
│   ├── buggy-regression-tests/<snapshot>/
│   └── fixed-verification/<snapshot>/
├── final-dependency-sync.log
├── <case-id>.debugging-framework.json
├── <case-id>.failure.log
└── error.log                        # Traceback khi prepare lỗi
```

Manifest ghi commit, source files, image ID, dependency commits, jobs/timeout,
stage/trạng thái, kết quả test buggy/fixed, tập regression đã chọn và lý do loại
test. Config và failure log cũng được sao lưu tại đây sau khi được tạo. Nếu lỗi
sớm, thư mục chỉ chứa log/kết quả của các bước đã chạy; config chưa được tạo sẽ
không xuất hiện. Runner in đường dẫn log ngay lúc bắt đầu. Khi dùng
`--inputs-root` tùy chỉnh, `prepare-logs` nằm cạnh thư mục inputs đó.

Sau mỗi lần chạy target/case, adapter chụp riêng các file `.txt.raw`, `.txt` và
`.xml` do `cpp/build-support/run-test.sh` tạo (nếu có), kèm `test.json` chứa test
ID, command, return code và đường dẫn file gốc. Artifact cũ của chính target
được xóa trước khi chạy để không lấy nhầm log từ test trước. Các snapshot được
giữ trong `test-artifacts/<tên-log>/<snapshot>/`, ngay cả sau khi source buggy
hoặc fixed đã bị dọn; chạy case kế tiếp không ghi đè snapshot trước.

Chỉ khi test timeout, nội dung `.txt.raw` mới được nối vào output với nhãn
`===== artifact: ... =====`, để `.failure.log` có cả chẩn đoán wrapper chưa kịp
xuất ra stdout. Test chạy xong thì stdout đã đủ; `.txt`, `.xml` và `.txt.raw`
chỉ nằm trong snapshot ở `prepare-logs`. Timeout ở adapter dừng cả nhóm
process trước khi chụp file. Nếu hết `--command-timeout` ở cấp Docker, runner
thử pause container, copy log/snapshot từ workspace (gồm workspace tạm trên
macOS), rồi dọn container; dữ liệu cứu được nằm trong snapshot `interrupted-*`
và command log. Lỗi copy được ghi vào command log; dữ liệu chưa flush xuống
file hoặc container đã bị xóa thì không thể khôi phục. Prepare lỗi ở cấp này
không publish input chưa được xác minh.

Chạy kiểm tra logic prepare bằng runtime giả lập, không cần Docker:

```bash
python3 -m unittest discover \
  -s defectsc_tpl/projects_v1/apache___arrow \
  -p test_arrow_debugging_case.py -v
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
  --agent-timeout 7200
```
