# Chạy SPIRV-Tools metadata

Thực hiện các lệnh dưới đây từ thư mục `defects4c/`.

## Bước 1: Build Docker image

```bash
docker build -f Dockerfile.spirv-tools \
  -t spirv-tools/defect4c:latest .
```

## Bước 2: Tạo container

```bash
docker run -d \
  --name my_defects4c_spirv-tools \
  --ipc=host \
  -v "$(pwd)/defectsc_tpl:/src" \
  -v "$(pwd)/out_tmp_dirs:/out" \
  -v "$(pwd)/patche_dirs:/patches" \
  -v "$(pwd)/unified_debugging:/unified_debugging" \
  -v "$(pwd)/../Unified-Debugging:/udbg" \
  spirv-tools/defect4c:latest sleep infinity
```

Nếu container đã tồn tại:

```bash
docker start my_defects4c_spirv-tools
```

## Bước 3: Clone các revision

```bash
docker exec my_defects4c_spirv-tools bash -lc \
  'cd /src && bash bulk_git_clone_v2.sh mini KhronosGroup___SPIRV-Tools'
```

Có thể bỏ qua bước này nếu dùng tùy chọn `--clone` ở bước chạy metadata.

## Bước 4: Kiểm tra danh sách bug

```bash
docker exec my_defects4c_spirv-tools bash -lc '
  cd /src/projects_v1/KhronosGroup___SPIRV-Tools
  python3 build_meta_spirv.py --list
'
```

Mặc định có 10 bug với `status_manual == 1`.

## Bước 5: Chạy thử một bug

Dùng `regression` để chỉ chạy từng GoogleTest case liên quan đến bug:

```bash
docker exec my_defects4c_spirv-tools bash -lc '
  cd /src/projects_v1/KhronosGroup___SPIRV-Tools
  python3 build_meta_spirv.py \
    --sha 6a9be627c760cf1efa43d155d4e6ee5e801deba3 \
    --metadata-dir /out/unified_debugging/spirv-tools/metadata \
    --raw-dir /out/unified_debugging/spirv-tools/raw \
    --test-scope regression \
    --jobs 2 \
    --clone
'
```

## Bước 6: Chạy cả 10 bug, tối đa 200 case mỗi bug

Lệnh này đặt regression case lên đầu, ưu tiên các case cùng test suite, rồi bổ
sung case khác trong `test_opt` cho đến tối đa 200 case. Coverage vẫn được thu
riêng cho từng case:

```bash
docker exec my_defects4c_spirv-tools bash -lc '
  cd /src/projects_v1/KhronosGroup___SPIRV-Tools
  python3 build_meta_spirv.py \
    --metadata-dir /out/unified_debugging/spirv-tools/metadata \
    --raw-dir /out/unified_debugging/spirv-tools/raw \
    --test-scope target \
    --max-tests 200 \
    --jobs 2 \
    --skip-if-exists \
    --clone
'
```

`--skip-if-exists` cho phép chạy lại lệnh và bỏ qua metadata đã hoàn chỉnh.
`--max-tests` mặc định là 200; dùng `0` chỉ khi thật sự muốn chạy không giới
hạn. `target` với cap 200 cung cấp regression cùng một tập passing-test coverage
cho fault localization mà không phải chạy hơn 2.000 case của `test_opt`.

Đối với 200 case đã chọn, coverage không lọc (gồm cả
test/generated/dependency) được lưu trong `raw/`. File trong `metadata/` chỉ
giữ coverage của production code dưới `source/` và `include/spirv-tools/`.

## Bước 7: Sinh `test_info`

```bash
docker exec my_defects4c_spirv-tools bash -lc '
  cd /src/projects_v1/KhronosGroup___SPIRV-Tools
  python3 test_info.py \
    --metadata-dir /out/unified_debugging/spirv-tools/metadata \
    --repo-root /out/KhronosGroup___SPIRV-Tools \
    --output /out/unified_debugging/spirv-tools/metadata/spirv-tools_test_info.json \
    --update-metadata
'
```

## Bước 8: Kiểm tra output

```text
out_tmp_dirs/unified_debugging/spirv-tools/
├── metadata/
│   ├── *_meta.json
│   ├── bugs_list_new.json
│   └── spirv-tools_test_info.json
└── raw/
    └── *_meta.json
```

Mỗi test có ID dạng:

```text
spirv-tools-test_opt::Optimizer.RemoveNop
```

Coverage và kết quả buggy/fixed được lưu riêng cho từng GoogleTest case.

## Chọn phạm vi test

```text
regression  Chỉ case liên quan đến bug; phù hợp để pilot nhanh.
target      Regression trước, sau đó case trong test_opt tới --max-tests.
all         Regression trước, sau đó mọi CTest target tới --max-tests.
```

Lựa chọn khuyến nghị để tạo dataset chính thức:

```bash
--test-scope target --max-tests 200
```

Các tùy chọn chẩn đoán nhanh:

```bash
python3 build_meta_spirv.py --limit 1 --test-scope regression --single-run --skip-coverage --clone
```
