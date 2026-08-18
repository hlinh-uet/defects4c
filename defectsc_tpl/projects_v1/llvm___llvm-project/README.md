# LLVM — Debugging-Framework workspace lớn

Adapter này giữ **toàn bộ `llvm-project` workspace** và đóng gói từng defect
thành input schema v6 cho Debugging-Framework. Git history không được chép vào
input; source tree vẫn đầy đủ.

## Tài nguyên

LLVM nặng hơn đáng kể so với libyang. Nên chuẩn bị tối thiểu:

- 100 GiB dung lượng trống cho cache, hai lượt build khi `prepare`, và lượt
  build của Framework;
- 16 GiB RAM; 32 GiB phù hợp hơn;
- timeout ít nhất 7200 giây cho mỗi build command.

Runner mặc định dùng `Release` cộng `LLVM_ENABLE_ASSERTIONS=ON` và build target
`llvm-test-depends`. Cách này giữ hành vi kiểm thử cần thiết nhưng nhỏ hơn một
full Debug build. Dùng `--build-type Debug` nếu cần stress cả build artifact.

## Build image

Từ thư mục `defects4c`:

```bash
docker build -f Dockerfile.llvm -t llvm/defect4c:latest .
```

Image dùng Ubuntu 20.04 để giữ Python 2 cho các revision LLVM cũ, đồng thời cài
CMake 3.27 để cấu hình được revision mới trong dataset.

## Chọn case

```bash
python3 defectsc_tpl/projects_v1/llvm___llvm-project/run_debugging_case.py \
  --list
```

`type.id` bị trùng giữa nhiều case, vì vậy nên chọn bằng SHA. Case mới nhất:

```bash
SHA=ab3fdbdfbe7edc62049c602d87be91c3ad3f5e3b
```

## Chuẩn bị input

```bash
python3 defectsc_tpl/projects_v1/llvm___llvm-project/run_debugging_case.py \
  prepare --sha "$SHA"
```

`prepare` thực hiện:

1. Tạo một partial Git cache dùng chung ở
   `out_tmp_dirs/llvm___llvm-project/source-cache`.
2. Export toàn bộ fixed tree rồi overlay `files.src` từ `commit_before` để tạo
   buggy tree.
3. Configure LLVM và build `llvm-test-depends` trong image.
4. Chạy từng test defect khai báo bằng `llvm-lit`.
5. Discover test trong `llvm/test` mà không thực thi full suite, rồi chọn cố định
   tối đa 70 test bổ sung theo `case_id`.
6. Chạy cùng tập test bổ sung trên buggy và fixed. Chỉ test pass trên cả hai
   revision mới được yêu cầu bởi regression command; các baseline failure được
   ghi thành `--exclude-test` trong metadata và không làm `prepare` thất bại.
7. Xóa buggy build, build fixed tree và chỉ giữ test defect có oracle
   `FAIL(buggy) -> PASS(fixed)`.
8. Xóa build artifact trước khi publish input.

Output:

```text
out_tmp_dirs/debugging_framework/llvm/inputs/
├── B__ab3fdbdfbe7e/                       # full buggy llvm-project tree
├── B__ab3fdbdfbe7e.debugging-framework.json
└── B__ab3fdbdfbe7e.failure.log
```

Nếu ổ hệ thống không đủ chỗ:

```bash
python3 defectsc_tpl/projects_v1/llvm___llvm-project/run_debugging_case.py \
  prepare --sha "$SHA" \
  --cache-root /Volumes/LARGE_DISK/defects4c-cache \
  --inputs-root /Volumes/LARGE_DISK/llvm-inputs
```

## Doctor và repair

```bash
python3 defectsc_tpl/projects_v1/llvm___llvm-project/run_debugging_case.py \
  doctor --sha "$SHA"

python3 defectsc_tpl/projects_v1/llvm___llvm-project/run_debugging_case.py \
  repair --sha "$SHA" --command-timeout 7200
```

Hoặc chạy `prepare -> doctor -> repair`:

```bash
python3 defectsc_tpl/projects_v1/llvm___llvm-project/run_debugging_case.py \
  trial --sha "$SHA" --command-timeout 7200
```

Config không chạy toàn bộ `llvm/test`. `target_test` chạy test defect được khai
báo; `regression_test` chạy tối đa 70 test bổ sung đã chọn cố định và loại các
test không pass trên cả buggy lẫn fixed. Image ID đã inspect được ghi vào config
để Framework không âm thầm pull hoặc đổi environment.

Input LLVM là source export không có `.git`, nên adapter đánh dấu nó
`workspace.disposable=true` và cho phép Framework tạo một baseline Git tạm ngay
trong chính thư mục input. Framework không copy thêm full source tree; nó reset
baseline giữa các attempt và xóa `.git` tạm khi kết thúc. Config của input đã
prepare bằng phiên bản adapter cũ được tự bổ sung contract này khi gọi
`prepare`, `doctor`, `show`, `trial` hoặc `repair`, không cần build lại case.

## Giới hạn hiện tại

- Dataset trải dài 2014–2023. Một số revision cũ có thể cần bổ sung dependency
  hoặc CMake compatibility flag riêng; runner fail closed và không xuất input
  nếu build/test oracle chưa được quan sát.
- `--all` chạy 143 case và rất tốn thời gian. Cache source được dùng chung,
  nhưng mỗi case vẫn phải build buggy và fixed.
- Pipeline này tạo input cho Debugging-Framework. Metadata coverage kiểu
  Unified-Debugging là workflow riêng và chưa được giả lập bằng dữ liệu giả.
