# CESNET/libyang — thử Debugging-Framework

Mục tiêu của adapter này rất hẹp:

1. Defects4C tạo đúng input của một project lỗi.
2. Debugging-Framework nhận input đó bằng public CLI và tự quản lý output.

## File trong thư mục

| File | Mục đích |
|---|---|
| `run_debugging_case.py` | Tạo input và gọi `debugging-framework doctor/repair` |
| `bugs_list_new.json` | Danh sách 15 defect, commit và failing target |
| `project.json` | URL repository libyang |
| `build_meta_libyang.py` | Pipeline metadata/coverage cũ, không dùng khi thử Framework |
| `test_info.py` | Helper cho metadata cũ, không dùng khi thử Framework |

## Chuẩn bị

Docker daemon phải chạy và image phải tồn tại:

```bash
cd /Users/linhnh/Developer/Debugging/defects4c

docker image inspect libyang/defect4c:latest
```

Nếu chưa có image:

```bash
docker build -f Dockerfile.libyang -t libyang/defect4c:latest .
```

Container nền `my_defects4c_libyang` không được dùng trong workflow này. Có thể
để nó chạy; adapter và Framework dùng image để tạo container tạm khi build/test.

## Bước 1: Defects4C chỉ tạo input

Đứng tại thư mục `defects4c`:

```bash
cd /Users/linhnh/Developer/Debugging/defects4c

python \
  defectsc_tpl/projects_v1/CESNET___libyang/run_debugging_case.py \
  prepare \
  --sha 92cc8517fcb85dcfbb93842758571f721c49c9cb
```

Đầu ra của bước này chỉ gồm input:

```text
out_tmp_dirs/debugging_framework/libyang/inputs/
├── D.1__92cc8517fcb8/               # buggy project root
│   ├── CMakeLists.txt
│   ├── src/
│   └── tests/
├── D.1__92cc8517fcb8.debugging-framework.json  # build/test/environment contract
└── D.1__92cc8517fcb8.failure.log               # failing-test output
```

Không tạo `results/`, `outputs/`, manifest hay audit logs trong Defects4C.
Muốn tạo lại input, thêm `--force`. Nếu image tag đã được build lại thành digest
mới, adapter cũng yêu cầu `--force` để không tái sử dụng config ghim image cũ.

Config sinh ra dùng `schema_version=6` và lưu đầy đủ contract mà Framework cần:

- `setup`: configure CMake với test enabled;
- `build`: build bằng Ninja;
- `target_test`: CTest `-R ^{test_id}$`, chỉ là bước fail-fast tùy chọn;
- `regression_test`: chạy toàn bộ CTest suite ngoại trừ các test không có outcome
  `passed` trên fixed commit (bao gồm `failed`, `error` và `skipped`);
- `repair.failing_tests` và image/runtime đã chuẩn bị.
- `workspace`: cho phép Framework tạo Git baseline tạm vì input đã bỏ `.git`.

Trong lúc `prepare`, adapter luôn chạy cả buggy tree và fixed tree. Chỉ target có
outcome trực tiếp `FAIL(buggy) -> PASS(fixed)` được ghi vào
`repair.failing_tests`; kết quả target trên fixed không được suy ra từ một lần
chạy full suite khác. Mọi test không pass trên fixed bị loại khỏi regression
contract. Patch chỉ đạt `status=plausible` khi target và toàn bộ suite hợp lệ này
pass. Các input cũ cần chạy lại `prepare --force` để nhận contract đã lọc và
workspace contract mới.

## Bước 2: xem đúng public CLI

```bash
python \
  defectsc_tpl/projects_v1/CESNET___libyang/run_debugging_case.py \
  show \
  --sha 92cc8517fcb85dcfbb93842758571f721c49c9cb
```

## Bước 3: chạy Debugging-Framework trực tiếp

Vẫn đứng tại thư mục `defects4c`:

```bash
PROJECT=out_tmp_dirs/debugging_framework/libyang/inputs/D.1__92cc8517fcb8
CONFIG=out_tmp_dirs/debugging_framework/libyang/inputs/D.1__92cc8517fcb8.debugging-framework.json
FAILURE=out_tmp_dirs/debugging_framework/libyang/inputs/D.1__92cc8517fcb8.failure.log
FRAMEWORK=../Debugging-Framework/.venv/bin/debugging-framework

"$FRAMEWORK" doctor "$PROJECT" --config "$CONFIG"

"$FRAMEWORK" repair \
  --project "$PROJECT" \
  --config "$CONFIG" \
  --failure-output "$FAILURE"
```

Không truyền `--results-dir` hoặc `--output`. Debugging-Framework tự chọn nơi
lưu theo cấu hình của nó. Với checkout hiện tại, `.env` của Framework đặt
`DEBUGGING_RESULTS_DIR=./experiments`, nên kết quả case này nằm tại:

```text
/Users/linhnh/Developer/Debugging/Debugging-Framework/experiments/D.1__92cc8517fcb8/
├── patch.diff
├── result.json
├── run_manifest.json
└── attempts/
```

## Runner gọi hộ Framework

Hai lệnh sau chỉ gọi lại public CLI ở trên, không tự quản lý output:

```bash
python defectsc_tpl/projects_v1/CESNET___libyang/run_debugging_case.py \
  doctor --sha 92cc8517fcb85dcfbb93842758571f721c49c9cb

python defectsc_tpl/projects_v1/CESNET___libyang/run_debugging_case.py \
  repair --sha 92cc8517fcb85dcfbb93842758571f721c49c9cb
```

Hoặc chạy `prepare → doctor → repair`:

```bash
python defectsc_tpl/projects_v1/CESNET___libyang/run_debugging_case.py \
  trial --sha 92cc8517fcb85dcfbb93842758571f721c49c9cb
```

Khi không truyền các cờ policy, Framework dùng cấu hình của chính nó. Hiện tại
mặc định là `attempts=2`, model `gpt-5.6-sol`, timeout 1800 giây và `jobs=0`
(tự chọn theo CPU cho build tự phát hiện). Build command của input libyang vẫn
ghi `--parallel 4`, là thông số đóng gói project khi chạy `prepare`. Có thể ghi
đè policy của Framework bằng CLI khi cần.

## Xác nhận buggy version

Adapter checkout toàn bộ repository tại `commit_after`, sau đó ghi đè các file
trong `files.src` bằng nội dung tại `commit_before`. Với case trên, file được
hoàn nguyên là `src/tree_schema_compile.c`; target
`src_tree_schema_compile` được chạy thật và output fail được lưu vào file
`.failure.log`. Adapter cũng build fixed commit, loại mọi target không pass trực
tiếp trên fixed và loại mọi fixed-nonpassing test khỏi `regression_test`.
